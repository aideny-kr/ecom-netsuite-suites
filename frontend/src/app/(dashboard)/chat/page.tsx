"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { consumeChatStream } from "@/lib/chat-stream";
import type { FinancialReportData, DataTableData, TaskOutputData, StreamBlock } from "@/lib/chat-stream";
import type { ChartData } from "@/lib/types";
import type { ChatSession, ChatSessionDetail, ChatMessage, StreamingToolCall } from "@/lib/types";
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
  const newSessionParam = searchParams?.get("new_session") || null;
  const prefillSentRef = useRef(false);
  const [agentTab, setAgentTab] = useState<"chat" | "config">("chat");
  const [templateFile, setTemplateFile] = useState<{ id: string; filename: string } | null>(null);
  const { data: agents = [] } = useAgents();

  // Reset tab when agent changes
  useEffect(() => { setAgentTab("chat"); }, [pinnedAgentId]);
  const [error, setError] = useState<string | null>(null);
  const [pendingMessage, setPendingMessage] = useState<string | null>(null);
  const [streamBlocks, setStreamBlocks] = useState<StreamBlock[]>([]);
  const [streamingMessage, setStreamingMessage] = useState<ChatMessage | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const isStreamingRef = useRef(false);
  const [activeSourcePick, setActiveSourcePick] = useState<"netsuite" | "bigquery" | null>(null);
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
  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activeRunRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const handleStop = useCallback(async () => {
    const runId = activeRunRef.current;
    if (!runId) return;
    try {
      await apiClient.post(`/api/v1/chat/runs/${runId}/cancel`, {});
    } catch {
      // Ignore cancel errors
    }
  }, []);

  const appendTextBlock = useCallback((toFlush: string) => {
    setStreamBlocks(prev => {
      const last = prev[prev.length - 1];
      if (last && last.type === "text") {
        return [...prev.slice(0, -1), { ...last, content: last.content + toFlush }];
      }
      return [...prev, { type: "text" as const, content: toFlush, id: `text-${Date.now()}` }];
    });
  }, []);

  const flushBuffer = useCallback(() => {
    rafRef.current = null;
    if (flushTimerRef.current) { clearTimeout(flushTimerRef.current); flushTimerRef.current = null; }
    if (bufferRef.current.length === 0) return;
    const text = bufferRef.current.join("");
    // Prefer word/sentence boundaries to prevent mid-word rendering
    const boundaryMatch = text.match(/^([\s\S]*[\s.!?:;\n,\-—])([^\s.!?:;\n,\-—]*)$/);
    if (boundaryMatch && boundaryMatch[2].length > 0 && boundaryMatch[2].length < 40) {
      bufferRef.current = [boundaryMatch[2]];
      appendTextBlock(boundaryMatch[1]);
      // Start safety timer for remainder — force flush if no new chunks arrive
      flushTimerRef.current = setTimeout(forceFlush, 100);
    } else {
      bufferRef.current = [];
      appendTextBlock(text);
    }
  }, [appendTextBlock]);

  const forceFlush = useCallback(() => {
    flushTimerRef.current = null;
    if (bufferRef.current.length === 0) return;
    const text = bufferRef.current.join("");
    bufferRef.current = [];
    appendTextBlock(text);
  }, [appendTextBlock]);

  const { data: workspaces = [] } = useWorkspaces();

  const { data: sessions = [] } = useQuery<ChatSession[]>({
    queryKey: ["chat-sessions", "main"],
    queryFn: () => apiClient.get<ChatSession[]>("/api/v1/chat/sessions"),
    // Poll every 5s when any session is running, so sidebar indicator updates
    refetchInterval: (query) => {
      const data = query.state.data;
      const hasRunning = data?.some((s) => s.status === "running" || s.status === "cancelling");
      return hasRunning ? 5000 : false;
    },
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

  // When switching to a session that finished a background run, refetch to show the result.
  // We intentionally do NOT reconnect to mid-stream runs — the completed response
  // will appear when the session detail is refetched after the run finishes.
  useEffect(() => {
    if (!activeSessionId) return;
    queryClient.invalidateQueries({ queryKey: ["chat-session", activeSessionId] });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId]);

  // ── Shared stream consumption ───────────────────────────────────────────
  // Used by both handleSend (new message) and reconnection (navigated back
  // to a session with an active run). Extracted to avoid duplicating handlers.
  const connectToRunStream = useCallback(
    async (runId: string, sessionId: string) => {
      const controller = new AbortController();
      abortRef.current = controller;
      activeRunRef.current = runId;
      isStreamingRef.current = true;
      setIsStreaming(true);
      setStreamBlocks([]);
      setStreamingMessage(null);

      try {
        const res = await apiClient.streamGet(
          `/api/v1/chat/runs/${runId}/stream?last_id=0`,
          controller.signal,
        );
        await consumeChatStream(res, {
          onText: (chunk) => {
            bufferRef.current.push(chunk);
            if (rafRef.current === null) {
              rafRef.current = requestAnimationFrame(flushBuffer);
            }
            if (flushTimerRef.current) clearTimeout(flushTimerRef.current);
            flushTimerRef.current = setTimeout(forceFlush, 100);
          },
          onToolStatus: () => {},
          onFinancialReport: (data) => {
            setFinancialReport(data);
            setStreamBlocks(prev => [...prev, { type: "financial_report" as const, data, id: `fr-${Date.now()}` }]);
          },
          onDataTable: (data) => {
            setDataTable(data);
            setStreamBlocks(prev => [...prev, { type: "data_table" as const, data, id: `dt-${Date.now()}` }]);
          },
          onChart: (data) => {
            setCharts((prev) => [...prev, data]);
            setStreamBlocks(prev => [...prev, { type: "chart" as const, data, id: `chart-${Date.now()}` }]);
          },
          onTaskOutput: (data) => {
            setTaskOutput(data);
            setStreamBlocks(prev => [...prev, { type: "task_output" as const, data, id: `to-${Date.now()}` }]);
          },
          onToolStart: (tool_name, tool_input, step) => {
            if (bufferRef.current.length > 0) {
              const text = bufferRef.current.join("");
              bufferRef.current = [];
              if (text.trim()) {
                setStreamBlocks(prev => {
                  const last = prev[prev.length - 1];
                  if (last && last.type === "text") {
                    return [...prev.slice(0, -1), { ...last, content: last.content + text }];
                  }
                  return [...prev, { type: "text" as const, content: text, id: `text-${Date.now()}` }];
                });
              }
            }
            setStreamBlocks(prev => [...prev, {
              type: "tool" as const,
              tool: { tool_name, tool_input, step, status: "running" as const },
              id: `tool-${step}`,
            }]);
          },
          onToolEnd: (tool_name, step, duration_ms, success, result_summary) => {
            setStreamBlocks(prev => prev.map(block =>
              block.type === "tool" && block.tool.step === step
                ? { ...block, tool: { ...block.tool, status: (success ? "complete" : "error") as StreamingToolCall["status"], duration_ms, success, result_summary } }
                : block
            ));
          },
          onError: (streamError) => {
            setError(streamError);
            if (abortRef.current) {
              abortRef.current.abort();
              abortRef.current = null;
            }
          },
          onMessage: (message) => {
            setFinancialReport((current) => {
              if (current) financialReportsRef.current.set(message.id, current);
              return null;
            });
            setDataTable((current) => {
              if (current) dataTablesRef.current.set(message.id, current);
              return null;
            });
            setCharts((current) => {
              if (current.length > 0) chartsRef.current.set(message.id, current);
              return [];
            });
            setTaskOutput((current) => {
              if (current) taskOutputsRef.current.set(message.id, current);
              return null;
            });
            setStreamingMessage(message);
            setStreamBlocks([]);
          },
        });
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        if (err instanceof Error && err.message.includes("aborted")) return;
        const message = err instanceof Error ? err.message : "Failed to stream response.";
        if (!message.includes("already in progress")) {
          setError(message);
        }
      } finally {
        activeRunRef.current = null;
        if (rafRef.current !== null) { cancelAnimationFrame(rafRef.current); rafRef.current = null; }
        if (flushTimerRef.current) { clearTimeout(flushTimerRef.current); flushTimerRef.current = null; }
        if (bufferRef.current.length > 0) {
          const remaining = bufferRef.current.join("");
          bufferRef.current = [];
          if (remaining.trim()) {
            setStreamBlocks(prev => {
              const last = prev[prev.length - 1];
              if (last && last.type === "text") {
                return [...prev.slice(0, -1), { ...last, content: last.content + remaining }];
              }
              return [...prev, { type: "text" as const, content: remaining, id: `text-final` }];
            });
          }
        }
        try {
          await queryClient.invalidateQueries({ queryKey: ["chat-session", sessionId] });
          await queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
        } catch { /* non-critical */ }
        isStreamingRef.current = false;
        setIsStreaming(false);
        setPendingMessage(null);
        setStreamBlocks([]);
        setStreamingMessage(null);
        setFinancialReport(null);
        setDataTable(null);
        setCharts([]);
        setTaskOutput(null);
        setActiveSourcePick(null);
      }
    },
    [flushBuffer, forceFlush, queryClient],
  );

  // ── Reconnect to active run on session switch ─────────────────────────
  // When navigating to a session with a running agent, reconnect to the SSE
  // stream so the user sees live progress (text, tool cards, data tables).
  // Prevents "looks dead" when navigating away and back mid-run.
  useEffect(() => {
    if (!sessionDetail?.active_run_id) return;
    if (sessionDetail.status !== "running") return;
    if (isStreamingRef.current) return; // Already consuming (we started this run)

    const runId = sessionDetail.active_run_id;
    const sessionId = activeSessionId!;

    connectToRunStream(runId, sessionId);

    // Cleanup: abort on unmount or session change
    return () => {
      if (abortRef.current) {
        abortRef.current.abort();
        abortRef.current = null;
      }
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionDetail?.active_run_id, sessionDetail?.status, activeSessionId]);

  const handleSend = useCallback(
    async (content: string, fileId?: string, opts: { source_pick?: "netsuite" | "bigquery" } = {}) => {
      if (isStreamingRef.current || createSession.isPending) return;
      setError(null);
      setPendingMessage(content);
      isStreamingRef.current = true;
      setIsStreaming(true);
      setStreamBlocks([]);
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
          isStreamingRef.current = false;
          setIsStreaming(false);
          setError("Failed to create chat session.");
          return;
        }
      }

      try {
        // Step 1: Submit message → get run_id
        const msgBody: Record<string, unknown> = {
          content,
          agent_id: pinnedAgentId || undefined,
          file_id: fileId || undefined,
        };
        if (opts.source_pick) msgBody.source_pick = opts.source_pick;
        const { run_id } = await apiClient.post<{ run_id: string }>(
          `/api/v1/chat/sessions/${sessionId}/messages`,
          msgBody,
        );

        // Message is now saved in DB — refetch so the persisted message appears,
        // then clear the optimistic pending copy. Clearing AFTER refetch prevents
        // the brief gap where neither the pending nor persisted message renders.
        await queryClient.invalidateQueries({ queryKey: ["chat-session", sessionId] });
        setPendingMessage(null);

        // Step 2: Connect to SSE stream for this run (shared with reconnection)
        await connectToRunStream(run_id, sessionId);
      } catch (err: unknown) {
        // AbortController.abort() throws — this is expected on session switch, not an error
        if (err instanceof DOMException && err.name === "AbortError") return;
        if (err instanceof Error && err.message.includes("aborted")) return;
        const message = err instanceof Error ? err.message : "Failed to send message. Please try again.";
        if (message.includes("already in progress")) {
          setError("A response is already in progress for this session. Please wait or stop it first.");
        } else {
          setError(message);
        }
      }
    },
    [activeSessionId, createSession, flushBuffer, queryClient, pinnedAgentId],
  );

  const handleSourcePick = useCallback(
    async (messageId: string, source: "netsuite" | "bigquery") => {
      const pickerMsg = sessionDetail?.messages.find((m) => m.id === messageId);
      const so = pickerMsg?.structured_output as { user_question?: string } | null | undefined;
      const originalQuestion = so?.user_question;
      if (!originalQuestion) {
        console.warn("[source-picker] no user_question in placeholder message");
        return;
      }
      // Optimistic update: mark the picker card as selected immediately so
      // the UI reflects the click without waiting for the backend round-trip.
      // The backend will persist the same `selected` value via orchestrator.
      if (activeSessionId && pickerMsg) {
        queryClient.setQueryData(
          ["chat-session", activeSessionId],
          (prev: ChatSessionDetail | undefined) => {
            if (!prev) return prev;
            return {
              ...prev,
              messages: prev.messages.map((m) => {
                if (m.id !== messageId) return m;
                const mSo = (m.structured_output as Record<string, unknown> | null) ?? {};
                return {
                  ...m,
                  structured_output: {
                    ...mSo,
                    selected: source,
                  } as unknown as ChatMessage["structured_output"],
                };
              }),
            };
          },
        );
      }
      // The picker's SSE stream may still be open (connectToRunStream hasn't
      // finished its finally block). Clear streaming state so handleSend's
      // isStreamingRef guard doesn't silently block the new request.
      if (abortRef.current) {
        abortRef.current.abort();
        abortRef.current = null;
      }
      isStreamingRef.current = false;
      setIsStreaming(false);
      setActiveSourcePick(source);
      await handleSend(originalQuestion, undefined, { source_pick: source });
    },
    [sessionDetail, handleSend, activeSessionId, queryClient],
  );

  const clearStreamingState = useCallback(() => {
    // Abort any in-flight SSE connection so old handlers stop firing
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    // Clear local streaming state — the old run continues server-side
    isStreamingRef.current = false;
    setIsStreaming(false);
    setStreamBlocks([]);
    setStreamingMessage(null);
    setFinancialReport(null);
    setDataTable(null);
    setCharts([]);
    setTaskOutput(null);
    setPendingMessage(null);
    setError(null);
    setActiveSourcePick(null);
  }, []);

  const handleNewChat = useCallback(() => {
    setActiveSessionId(null);
    clearStreamingState();
  }, [clearStreamingState]);

  const handleSelectSession = useCallback((sessionId: string) => {
    if (sessionId === activeSessionId) return;
    clearStreamingState();
    setActiveSessionId(sessionId);
  }, [activeSessionId, clearStreamingState]);

  // Auto-send prefill message from URL (e.g., from Recon "Investigate in Chat")
  useEffect(() => {
    if (!prefillMessage || prefillSentRef.current) return;
    const message = prefillMessage;
    prefillSentRef.current = true;

    const timer = setTimeout(async () => {
      if (newSessionParam === "true") {
        try {
          const session = await createSession.mutateAsync(
            pinnedAgentId ? { agent_id: pinnedAgentId } : undefined
          );
          setActiveSessionId(session.id);
        } catch {
          setError("Failed to create chat session.");
          return;
        }
      }
      handleSend(message);
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
        onSelectSession={handleSelectSession}
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
            activeSourcePick={activeSourcePick}
            streamBlocks={streamBlocks}
            streamingMessage={streamingMessage}
            financialReports={financialReportsRef.current}
            dataTables={dataTablesRef.current}
            chartsByMessage={chartsRef.current}
            taskOutputs={taskOutputsRef.current}
            pinnedAgentId={pinnedAgentId}
            agents={agents}
            agentTab={agentTab}
            onTabChange={setAgentTab}
            templateFile={templateFile}
            onTemplateUploaded={setTemplateFile}
            onRemoveTemplate={() => setTemplateFile(null)}
            onMentionClick={handleMentionClick}
            onSourcePick={handleSourcePick}
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
          onStop={handleStop}
          isLoading={isStreaming || createSession.isPending}
          isRunning={isStreaming}
          workspaceId={workspaces[0]?.id || null}
        />
      </div>
    </div>
  );
}
