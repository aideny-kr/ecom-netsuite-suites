"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { consumeChatStream } from "@/lib/chat-stream";
import type { FinancialReportData, DataTableData, TaskOutputData } from "@/lib/chat-stream";
import type { ChartData } from "@/lib/types";
import type { ChatSession, ChatSessionDetail, ChatMessage } from "@/lib/types";
import { SessionSidebar } from "@/components/chat/session-sidebar";
import { MessageList } from "@/components/chat/message-list";
import { ChatInput } from "@/components/chat/chat-input";
import { useWorkspaces } from "@/hooks/use-workspace";
import { useAgents } from "@/hooks/use-agents";
import { AlertCircle, X, PanelLeftOpen } from "lucide-react";

export default function ChatPage() {
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [chatSidebarCollapsed, setChatSidebarCollapsed] = useState(false);
  const searchParams = useSearchParams();
  const pinnedAgentId = searchParams?.get("agent") || null;
  const prefillMessage = searchParams?.get("prefill") || null;
  const prefillSentRef = useRef(false);
  const [agentTab, setAgentTab] = useState<"chat" | "config">("chat");
  const [templateFile, setTemplateFile] = useState<{ id: string; filename: string } | null>(null);
  const { data: agents = [] } = useAgents();

  // Reset tab when agent changes
  useEffect(() => { setAgentTab("chat"); }, [pinnedAgentId]);
  const [error, setError] = useState<string | null>(null);
  const [pendingMessage, setPendingMessage] = useState<string | null>(null);
  const [streamingContent, setStreamingContent] = useState<string | null>(null);
  const [streamingStatus, setStreamingStatus] = useState<string | null>(null);
  const [streamingSteps, setStreamingSteps] = useState<{ label: string; status: "complete" | "running" }[]>([]);
  const [streamingMessage, setStreamingMessage] = useState<ChatMessage | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [financialReport, setFinancialReport] = useState<FinancialReportData | null>(null);
  const financialReportsRef = useRef<Map<string, FinancialReportData>>(new Map());
  const [dataTable, setDataTable] = useState<DataTableData | null>(null);
  const dataTablesRef = useRef<Map<string, DataTableData>>(new Map());
  const [charts, setCharts] = useState<ChartData[]>([]);
  const chartsRef = useRef<Map<string, ChartData[]>>(new Map());
  const [taskOutput, setTaskOutput] = useState<TaskOutputData | null>(null);
  const taskOutputsRef = useRef<Map<string, TaskOutputData>>(new Map());
  const queryClient = useQueryClient();
  const router = useRouter();

  const bufferRef = useRef<string[]>([]);
  const rafRef = useRef<number | null>(null);

  const flushBuffer = useCallback(() => {
    if (bufferRef.current.length === 0) return;
    const text = bufferRef.current.join("");
    bufferRef.current = [];
    setStreamingContent((prev) => (prev || "") + text);
    rafRef.current = null;
  }, []);

  const { data: workspaces = [] } = useWorkspaces();

  const { data: sessions = [] } = useQuery<ChatSession[]>({
    queryKey: ["chat-sessions", "main"],
    queryFn: () => apiClient.get<ChatSession[]>("/api/v1/chat/sessions"),
  });

  const { data: sessionDetail, isLoading: isLoadingDetail } = useQuery<ChatSessionDetail>({
    queryKey: ["chat-session", activeSessionId],
    queryFn: () => apiClient.get<ChatSessionDetail>(`/api/v1/chat/sessions/${activeSessionId}`),
    enabled: !!activeSessionId,
  });

  const createSession = useMutation({
    mutationFn: (params?: { title?: string; agent_id?: string | null }) =>
      apiClient.post<ChatSession>("/api/v1/chat/sessions", {
        title: params?.title,
        agent_id: params?.agent_id || undefined,
      }),
    onSuccess: (session) => {
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
      setActiveSessionId(session.id);
    },
  });

  // Auto-select the most recent session on initial page load only
  const hasAutoSelected = useRef(false);
  useEffect(() => {
    if (!hasAutoSelected.current && !activeSessionId && sessions.length > 0) {
      setActiveSessionId(sessions[0].id);
      hasAutoSelected.current = true;
    }
  }, [sessions, activeSessionId]);

  // Hydrate structured output refs from persisted messages on session load
  const [, forceRender] = useState(0);
  useEffect(() => {
    if (!sessionDetail?.messages) return;
    let hydrated = false;
    for (const msg of sessionDetail.messages) {
      if (!msg.structured_output) continue;
      const { type, data, charts: persistedCharts } = msg.structured_output as any;
      if (type === "financial_report" && data) {
        financialReportsRef.current.set(msg.id, data as unknown as FinancialReportData);
        hydrated = true;
      } else if (type === "data_table" && data) {
        dataTablesRef.current.set(msg.id, data as unknown as DataTableData);
        hydrated = true;
      } else if (type === "chart" && data) {
        if (!chartsRef.current.has(msg.id)) {
          chartsRef.current.set(msg.id, [data as unknown as ChartData]);
          hydrated = true;
        }
      } else if (type === "task_output" && data) {
        taskOutputsRef.current.set(msg.id, data as any);
        hydrated = true;
      }
      // Hydrate persisted charts array (from v1.1 chart persistence)
      // Skip if charts already exist for this message (came from streaming)
      if (Array.isArray(persistedCharts) && persistedCharts.length > 0 && !chartsRef.current.has(msg.id)) {
        chartsRef.current.set(msg.id, persistedCharts as unknown as ChartData[]);
        hydrated = true;
      }
    }
    if (hydrated) forceRender((n) => n + 1);
  }, [sessionDetail]);

  const handleSend = useCallback(
    async (content: string, fileId?: string) => {
      if (isStreaming || createSession.isPending) return;
      setError(null);
      setPendingMessage(content);
      setIsStreaming(true);
      setStreamingContent("");
      setStreamingStatus(null);
      setStreamingMessage(null);
      setFinancialReport(null);
      setDataTable(null);
      setCharts([]);
      setTaskOutput(null);

      let sessionId = activeSessionId;
      if (!sessionId) {
        try {
          const session = await createSession.mutateAsync(pinnedAgentId ? { agent_id: pinnedAgentId } : undefined);
          sessionId = session.id;
        } catch {
          setPendingMessage(null);
          setIsStreaming(false);
          setError("Failed to create chat session.");
          return;
        }
      }

      try {
        const res = await apiClient.stream(
          `/api/v1/chat/sessions/${sessionId}/messages`,
          { content, agent_id: pinnedAgentId || undefined, file_id: fileId || undefined },
        );
        await consumeChatStream(res, {
          onText: (chunk) => {
            bufferRef.current.push(chunk);
            setStreamingStatus(null);
        setStreamingSteps([]);
            if (rafRef.current === null) {
              rafRef.current = requestAnimationFrame(flushBuffer);
            }
          },
          onToolStatus: (status) => {
            // Add newline before tool execution so next text block starts on new line
            setStreamingContent((prev) => prev && !prev.endsWith("\n") ? prev + "\n\n" : prev);
            setStreamingStatus(status);
            // Accumulate tool steps — mark previous as complete, add new as running
            setStreamingSteps((prev) => [
              ...prev.map((s) => ({ ...s, status: "complete" as const })),
              { label: status, status: "running" as const },
            ]);
          },
          onFinancialReport: (data) => setFinancialReport(data),
          onDataTable: (data) => setDataTable(data),
          onChart: (data) => setCharts((prev) => [...prev, data]),
          onTaskOutput: (data) => setTaskOutput(data),
          onError: (streamError) => setError(streamError),
          onMessage: (message) => {
            // Associate any in-flight financial report with this message
            setFinancialReport((current) => {
              if (current) {
                financialReportsRef.current.set(message.id, current);
              }
              return null;
            });
            // Associate any in-flight data table with this message
            setDataTable((current) => {
              if (current) {
                dataTablesRef.current.set(message.id, current);
              }
              return null;
            });
            // Associate any in-flight charts with this message
            setCharts((current) => {
              if (current.length > 0) {
                chartsRef.current.set(message.id, current);
              }
              return [];
            });
            // Associate any in-flight task output with this message
            setTaskOutput((current) => {
              if (current) {
                taskOutputsRef.current.set(message.id, current);
              }
              return null;
            });
            setStreamingMessage(message);
            setStreamingContent(null);
            setStreamingStatus(null);
        setStreamingSteps([]);
          },
        });
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : "Failed to send message. Please try again.";
        setError(message);
      } finally {
        // Flush any remaining buffered text and cancel pending RAF
        if (rafRef.current !== null) {
          cancelAnimationFrame(rafRef.current);
          rafRef.current = null;
        }
        if (bufferRef.current.length > 0) {
          const remaining = bufferRef.current.join("");
          bufferRef.current = [];
          setStreamingContent((prev) => (prev || "") + remaining);
        }
        // Refetch persisted messages BEFORE clearing streaming state
        // so there's no blank gap between streaming text disappearing
        // and the saved message appearing.
        // Use local sessionId (not activeSessionId) to avoid stale closure.
        await queryClient.invalidateQueries({ queryKey: ["chat-session", sessionId] });
        await queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
        setIsStreaming(false);
        setPendingMessage(null);
        setStreamingContent(null);
        setStreamingStatus(null);
        setStreamingSteps([]);
        setStreamingMessage(null);
        setFinancialReport(null);
        setDataTable(null);
        setCharts([]);
        setTaskOutput(null);
      }
    },
    [activeSessionId, createSession, flushBuffer, isStreaming, queryClient],
  );

  const handleNewChat = useCallback(() => {
    setActiveSessionId(null);
    setError(null);
    setPendingMessage(null);
  }, []);

  // Auto-send prefill message from URL (e.g., from Recon "Investigate in Chat")
  useEffect(() => {
    if (!prefillMessage || prefillSentRef.current) return;
    // Capture the message before clearing URL
    const message = prefillMessage;
    prefillSentRef.current = true;

    // Delay to let the page fully mount and session hooks initialize
    const timer = setTimeout(() => {
      handleSend(message);
      // Clear URL params after send to prevent re-send on back navigation
      router.replace(`/chat${pinnedAgentId ? `?agent=${pinnedAgentId}` : ""}`);
    }, 500);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefillMessage]);

  const handleMentionClick = useCallback(
    (filePath: string) => {
      const params = new URLSearchParams({ file: filePath });
      const workspaceId = workspaces[0]?.id;
      if (workspaceId) {
        params.set("workspace", workspaceId);
      }
      router.push(`/workspace?${params.toString()}`);
    },
    [router, workspaces],
  );

  return (
    <div className="flex h-full min-h-0 w-full min-w-0 animate-fade-in">
      <SessionSidebar
        variant="terminal"
        sessions={sessions}
        activeSessionId={activeSessionId}
        onSelectSession={setActiveSessionId}
        onNewChat={handleNewChat}
        collapsed={chatSidebarCollapsed}
        onToggle={() => setChatSidebarCollapsed(!chatSidebarCollapsed)}
      />
      <div className="relative flex min-w-0 flex-1 flex-col bg-[var(--chat-surface)]">
        {chatSidebarCollapsed && (
          <button
            onClick={() => setChatSidebarCollapsed(false)}
            className="absolute left-10 top-2 z-10 rounded-md p-1.5 text-[var(--chat-accent)] transition-colors hover:bg-[var(--chat-surface-mid)]"
            aria-label="Open chat history"
          >
            <PanelLeftOpen className="h-4 w-4" />
          </button>
        )}
        <div className="min-h-0 min-w-0 flex-1 overflow-hidden">
          <MessageList
            variant="terminal"
            messages={sessionDetail?.messages || []}
            isLoading={isLoadingDetail && !!activeSessionId}
            pendingUserMessage={pendingMessage}
            isWaitingForReply={isStreaming}
            streamingContent={streamingContent}
            streamingStatus={streamingStatus}
            streamingSteps={streamingSteps}
            streamingMessage={streamingMessage}
            financialReport={financialReport}
            financialReports={financialReportsRef.current}
            dataTable={dataTable}
            dataTables={dataTablesRef.current}
            charts={charts}
            chartsByMessage={chartsRef.current}
            taskOutput={taskOutput}
            taskOutputs={taskOutputsRef.current}
            pinnedAgentId={pinnedAgentId}
            agents={agents}
            agentTab={agentTab}
            onTabChange={setAgentTab}
            templateFile={templateFile}
            onTemplateUploaded={setTemplateFile}
            onRemoveTemplate={() => setTemplateFile(null)}
            onMentionClick={handleMentionClick}
            onImportanceOverride={(messageId, newTier) => {
              queryClient.setQueryData<ChatSessionDetail>(
                ["chat-session", activeSessionId],
                (old) => old ? {
                  ...old,
                  messages: old.messages.map((m) =>
                    m.id === messageId ? { ...m, query_importance: newTier } : m
                  ),
                } : old
              );
            }}
          />
        </div>
        {error && (
          <div className="mx-6 mb-2 flex items-center gap-2 rounded-sm border border-destructive/20 bg-destructive/5 px-4 py-2.5 text-[13px] text-destructive">
            <AlertCircle className="h-4 w-4 shrink-0" />
            <span className="flex-1">{error}</span>
            <button
              onClick={() => setError(null)}
              className="shrink-0 rounded-md p-0.5 hover:bg-destructive/20"
              aria-label="Dismiss error"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
        <ChatInput
          variant="terminal"
          onSend={handleSend}
          isLoading={isStreaming || createSession.isPending}
          workspaceId={workspaces[0]?.id || null}
        />
      </div>
    </div>
  );
}
