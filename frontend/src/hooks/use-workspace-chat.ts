"use client";

import { useState, useCallback, useEffect, useLayoutEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { consumeChatStream } from "@/lib/chat-stream";
import type { StreamBlock } from "@/lib/chat-stream";
import type { ChatMessage, ChatSession, ChatSessionDetail } from "@/lib/types";
import type { ChatSubmissionReceipt } from "@/lib/webmcp-chat";

export interface WorkspaceSendOptions { request_id?: string; assert_current?: () => void }

export function useWorkspaceChat(workspaceId: string | null) {
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [pendingMessage, setPendingMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const isStreamingRef = useRef(false);
  const activeRunRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const [streamBlocks, setStreamBlocks] = useState<StreamBlock[]>([]);
  const [streamingMessage, setStreamingMessage] = useState<ChatMessage | null>(null);
  const queryClient = useQueryClient();

  const bufferRef = useRef<string[]>([]);
  const rafRef = useRef<number | null>(null);

  const scopeRef = useRef(workspaceId);
  const mountedRef = useRef(true);
  const selectionVersion = useRef(0);
  const streamVersion = useRef(0);
  const pendingSubmission = useRef<symbol | null>(null);

  const invalidate = useCallback(() => {
    selectionVersion.current++;
    streamVersion.current++;
    pendingSubmission.current = null;
    abortRef.current?.abort();
    abortRef.current = null;
    activeRunRef.current = null;
    bufferRef.current = [];
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
  }, []);
  const clearLocal = useCallback(() => {
    invalidate();
    isStreamingRef.current = false;
    setIsStreaming(false);
    setPendingMessage(null);
    setStreamBlocks([]);
    setStreamingMessage(null);
    setError(null);
  }, [invalidate]);
  useLayoutEffect(() => {
    scopeRef.current = workspaceId;
    mountedRef.current = true;
    return () => { mountedRef.current = false; invalidate(); };
  }, [workspaceId, invalidate]);
  useEffect(() => { clearLocal(); setActiveSessionId(null); }, [workspaceId, clearLocal]);

  const flushBuffer = useCallback(() => {
    if (bufferRef.current.length === 0) return;
    const text = bufferRef.current.join("");
    bufferRef.current = [];
    setStreamBlocks(prev => {
      const last = prev[prev.length - 1];
      if (last && last.type === "text") {
        return [...prev.slice(0, -1), { ...last, content: last.content + text }];
      }
      return [...prev, { type: "text" as const, content: text, id: `text-${Date.now()}` }];
    });
    rafRef.current = null;
  }, []);

  // Show only sessions for this workspace
  const { data: sessions = [], isLoading: isLoadingSessions } = useQuery<ChatSession[]>({
    queryKey: ["chat-sessions", "workspace", workspaceId],
    queryFn: () =>
      apiClient.get<ChatSession[]>(`/api/v1/chat/sessions?workspace_id=${workspaceId}`),
    enabled: !!workspaceId,
  });

  const { data: sessionDetail, isLoading: isLoadingDetail } =
    useQuery<ChatSessionDetail>({
      queryKey: ["chat-session", activeSessionId],
      queryFn: () =>
        apiClient.get<ChatSessionDetail>(
          `/api/v1/chat/sessions/${activeSessionId}`,
        ),
      enabled: !!activeSessionId,
    });

  // Create workspace-scoped sessions so orchestrator injects workspace context
  const createSession = useMutation({
    mutationFn: (scope: string) =>
      apiClient.post<ChatSession>("/api/v1/chat/sessions", {
        workspace_id: scope,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
    },
  });

  const createConversation = useCallback(async () => {
    const scope = scopeRef.current;
    const version = selectionVersion.current;
    if (!scope || !mountedRef.current) throw new Error("Select a workspace first.");
    const session = await createSession.mutateAsync(scope);
    if (!mountedRef.current || scopeRef.current !== scope || selectionVersion.current !== version) {
      throw new Error("Workspace or conversation changed. Read current state before continuing.");
    }
    setActiveSessionId(session.id);
    return session;
  }, [createSession]);

  // ── Shared stream consumption ─────────────────────────────────────────
  // Background chat (PR #23) splits the request into two hops: POST
  // /messages returns {run_id, session_id} immediately, then the agent's
  // output streams from a separate GET endpoint keyed on run_id. The
  // workspace chat was previously reading the POST body as SSE — but
  // that body is plain JSON, so no text/message events fired and the
  // UI looked dead even though the assistant reply landed in the DB.
  // Mirrors connectToRunStream in app/(dashboard)/chat/page.tsx.
  const connectToRunStream = useCallback(
    async (runId: string, sessionId: string) => {
      const version = ++streamVersion.current;
      pendingSubmission.current = null;
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
        if (streamVersion.current !== version) return;
        await consumeChatStream(res, {
          onText: (chunk) => {
            if (streamVersion.current !== version) return;
            bufferRef.current.push(chunk);
            if (rafRef.current === null) {
              rafRef.current = requestAnimationFrame(flushBuffer);
            }
          },
          onToolStatus: () => {},
          onError: (streamError) => { if (streamVersion.current === version) setError(streamError); },
          onMessage: (message) => {
            if (streamVersion.current !== version) return;
            setStreamingMessage(message);
            setStreamBlocks([]);
          },
        });
      } catch (err: unknown) {
        if (streamVersion.current !== version) return;
        // AbortController.abort() throws — expected on session switch/unmount
        if (err instanceof DOMException && err.name === "AbortError") return;
        if (err instanceof Error && err.message.includes("aborted")) return;
        const message =
          err instanceof Error ? err.message : "Failed to load chat stream.";
        setError(message);
      } finally {
        if (streamVersion.current !== version) return;
        // Flush any remaining buffered text and cancel pending RAF
        if (rafRef.current !== null) {
          cancelAnimationFrame(rafRef.current);
          rafRef.current = null;
        }
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
          await queryClient.invalidateQueries({
            queryKey: ["chat-session", sessionId],
          });
          await queryClient.invalidateQueries({
            queryKey: ["chat-sessions"],
          });
        } catch {
          // Refetch failure is non-critical
        }
        if (streamVersion.current !== version) return;
        isStreamingRef.current = false;
        activeRunRef.current = null;
        abortRef.current = null;
        setIsStreaming(false);
        setPendingMessage(null);
        setStreamBlocks([]);
        setStreamingMessage(null);
      }
    },
    [flushBuffer, queryClient],
  );

  // ── Reconnect to active run on session switch ─────────────────────────
  // When navigating into a session whose agent is still running, attach
  // to the existing SSE stream from event 0 so the user sees the in-flight
  // output instead of a frozen "sent" bubble. Same pattern as the regular
  // chat page (app/(dashboard)/chat/page.tsx).
  useEffect(() => {
    if (!sessionDetail?.active_run_id) return;
    if (sessionDetail.status !== "running" && sessionDetail.status !== "cancelling") return;
    if (isStreamingRef.current) return;

    const runId = sessionDetail.active_run_id;
    const sessionId = activeSessionId;
    if (!sessionId) return;

    connectToRunStream(runId, sessionId);

    // Selection/unmount owns cancellation. Query status refreshes must not
    // abort a newer stream through the shared controller ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionDetail?.active_run_id, sessionDetail?.status, activeSessionId]);

  const handleSend = useCallback(
    async (content: string, fileId?: string, opts: WorkspaceSendOptions = {}): Promise<ChatSubmissionReceipt | undefined> => {
      const scope = scopeRef.current;
      if (!scope || !mountedRef.current) throw new Error("Select a workspace first.");
      opts.assert_current?.();
      if (opts.request_id && !activeSessionId) throw new Error("Create and select a conversation first.");
      const body = { content, file_id: fileId, request_id: opts.request_id };
      if (isStreamingRef.current || createSession.isPending) {
        if (opts.request_id && activeSessionId) {
          return apiClient.post<ChatSubmissionReceipt>(`/api/v1/chat/sessions/${activeSessionId}/messages`, body);
        }
        return;
      }
      const submission = Symbol("workspace submission");
      pendingSubmission.current = submission;
      const assertCurrent = () => {
        opts.assert_current?.();
        if (!mountedRef.current || scopeRef.current !== scope || pendingSubmission.current !== submission) {
          throw new Error("Workspace or conversation changed. Retry with the same request_id in its original conversation.");
        }
      };
      setError(null);
      setPendingMessage(content);
      isStreamingRef.current = true;
      setIsStreaming(true);
      try {
        const sessionId = activeSessionId || (await createConversation()).id;
        assertCurrent();
        const receipt = await apiClient.post<ChatSubmissionReceipt>(`/api/v1/chat/sessions/${sessionId}/messages`, body);
        assertCurrent();
        await queryClient.invalidateQueries({ queryKey: ["chat-session", sessionId] });
        assertCurrent();
        setPendingMessage(null);
        const stream = connectToRunStream(receipt.run_id, sessionId);
        if (!opts.request_id) await stream;
        return receipt;
      } catch (err) {
        // Only the still-owning submission may change this conversation's UI.
        if (pendingSubmission.current === submission && mountedRef.current) {
          pendingSubmission.current = null;
          isStreamingRef.current = false;
          setIsStreaming(false);
          setError(err instanceof Error ? err.message : "Failed to send message.");
        }
        if (opts.request_id) throw err;
      }
    },
    [activeSessionId, createSession.isPending, createConversation, connectToRunStream, queryClient],
  );

  const selectSession = useCallback((id: string | null) => {
    if (id === activeSessionId) return;
    clearLocal();
    setActiveSessionId(id);
  }, [activeSessionId, clearLocal]);
  const handleNewChat = useCallback(() => { clearLocal(); setActiveSessionId(null); }, [clearLocal]);
  const cancelActiveRun = useCallback(async (expectedId?: string) => {
    const runId = activeRunRef.current;
    if (!runId || (expectedId && expectedId !== runId)) throw new Error("No matching active workspace run.");
    return apiClient.post(`/api/v1/chat/runs/${runId}/cancel`, {});
  }, []);
  const handleStop = useCallback(async () => {
    try { await cancelActiveRun(); }
    catch { setError("Could not stop the response. Check run status and retry."); }
  }, [cancelActiveRun]);

  return {
    sessions,
    activeSessionId,
    setActiveSessionId: selectSession,
    sessionDetail,
    isLoadingDetail,
    isLoadingSessions,
    createConversation,
    cancelActiveRun,
    handleStop,
    activeRunId: activeRunRef.current,
    pendingMessage,
    error,
    setError,
    handleSend,
    handleNewChat,
    isSending: isStreaming || createSession.isPending,
    streamBlocks,
    streamingMessage,
  };
}
