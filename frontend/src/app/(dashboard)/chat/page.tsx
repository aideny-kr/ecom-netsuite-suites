"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { consumeChatStream } from "@/lib/chat-stream";
import type { FinancialReportData, DataTableData } from "@/lib/chat-stream";
import type { ChatSession, ChatSessionDetail, ChatMessage } from "@/lib/types";
import { SessionSidebar } from "@/components/chat/session-sidebar";
import { MessageList } from "@/components/chat/message-list";
import { ChatInput } from "@/components/chat/chat-input";
import { useWorkspaces } from "@/hooks/use-workspace";
import { AlertCircle, X } from "lucide-react";

export default function ChatPage() {
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pendingMessage, setPendingMessage] = useState<string | null>(null);
  const [streamingContent, setStreamingContent] = useState<string | null>(null);
  const [streamingStatus, setStreamingStatus] = useState<string | null>(null);
  const [streamingMessage, setStreamingMessage] = useState<ChatMessage | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [financialReport, setFinancialReport] = useState<FinancialReportData | null>(null);
  const financialReportsRef = useRef<Map<string, FinancialReportData>>(new Map());
  const [dataTable, setDataTable] = useState<DataTableData | null>(null);
  const dataTablesRef = useRef<Map<string, DataTableData>>(new Map());
  const queryClient = useQueryClient();
  const router = useRouter();
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
    mutationFn: (title?: string) =>
      apiClient.post<ChatSession>("/api/v1/chat/sessions", { title }),
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
      const { type, data } = msg.structured_output;
      if (type === "financial_report" && data) {
        financialReportsRef.current.set(msg.id, data as unknown as FinancialReportData);
        hydrated = true;
      } else if (type === "data_table" && data) {
        dataTablesRef.current.set(msg.id, data as unknown as DataTableData);
        hydrated = true;
      }
    }
    if (hydrated) forceRender((n) => n + 1);
  }, [sessionDetail]);

  const handleSend = useCallback(
    async (content: string) => {
      if (isStreaming || createSession.isPending) return;
      setError(null);
      setPendingMessage(content);
      setIsStreaming(true);
      setStreamingContent("");
      setStreamingStatus(null);
      setStreamingMessage(null);
      setFinancialReport(null);
      setDataTable(null);

      let sessionId = activeSessionId;
      if (!sessionId) {
        try {
          const session = await createSession.mutateAsync(undefined);
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
          { content },
        );
        await consumeChatStream(res, {
          onText: (chunk) => {
            setStreamingContent((prev) => (prev || "") + chunk);
            setStreamingStatus(null);
          },
          onToolStatus: (status) => setStreamingStatus(status),
          onFinancialReport: (data) => setFinancialReport(data),
          onDataTable: (data) => setDataTable(data),
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
            setStreamingMessage(message);
            setStreamingContent(null);
            setStreamingStatus(null);
          },
        });
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : "Failed to send message. Please try again.";
        setError(message);
      } finally {
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
        setStreamingMessage(null);
        setFinancialReport(null);
        setDataTable(null);
      }
    },
    [activeSessionId, createSession, isStreaming, queryClient],
  );

  const handleNewChat = useCallback(() => {
    setActiveSessionId(null);
    setError(null);
    setPendingMessage(null);
  }, []);

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
      />
      <div className="flex min-w-0 flex-1 flex-col bg-[var(--chat-surface)]">
        <div className="min-h-0 min-w-0 flex-1 overflow-hidden">
          <MessageList
            variant="terminal"
            messages={sessionDetail?.messages || []}
            isLoading={isLoadingDetail && !!activeSessionId}
            pendingUserMessage={pendingMessage}
            isWaitingForReply={isStreaming}
            streamingContent={streamingContent}
            streamingStatus={streamingStatus}
            streamingMessage={streamingMessage}
            financialReport={financialReport}
            financialReports={financialReportsRef.current}
            dataTable={dataTable}
            dataTables={dataTablesRef.current}
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
