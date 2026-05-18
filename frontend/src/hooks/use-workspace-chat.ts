"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { consumeChatStream } from "@/lib/chat-stream";
import type { StreamBlock } from "@/lib/chat-stream";
import type { ChatMessage, ChatSession, ChatSessionDetail } from "@/lib/types";

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
  const { data: sessions = [] } = useQuery<ChatSession[]>({
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
    mutationFn: () =>
      apiClient.post<ChatSession>("/api/v1/chat/sessions", {
        workspace_id: workspaceId,
      }),
    onSuccess: (session) => {
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
      setActiveSessionId(session.id);
    },
  });

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
          },
          onToolStatus: () => {},
          onError: (streamError) => setError(streamError),
          onMessage: (message) => {
            setStreamingMessage(message);
            setStreamBlocks([]);
          },
        });
      } catch (err: unknown) {
        // AbortController.abort() throws — expected on session switch/unmount
        if (err instanceof DOMException && err.name === "AbortError") return;
        if (err instanceof Error && err.message.includes("aborted")) return;
        const message =
          err instanceof Error ? err.message : "Failed to load chat stream.";
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
    if (sessionDetail.status !== "running") return;
    if (isStreamingRef.current) return;

    const runId = sessionDetail.active_run_id;
    const sessionId = activeSessionId;
    if (!sessionId) return;

    connectToRunStream(runId, sessionId);

    return () => {
      if (abortRef.current) {
        abortRef.current.abort();
        abortRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionDetail?.active_run_id, sessionDetail?.status, activeSessionId]);

  const handleSend = useCallback(
    async (content: string) => {
      if (isStreamingRef.current || createSession.isPending) return;
      setError(null);
      setPendingMessage(content);

      let sessionId = activeSessionId;
      if (!sessionId) {
        try {
          const session = await createSession.mutateAsync();
          sessionId = session.id;
        } catch {
          setPendingMessage(null);
          setError("Failed to create chat session.");
          return;
        }
      }

      try {
        // Step 1: POST /messages — returns {run_id, session_id}
        const { run_id } = await apiClient.post<{ run_id: string; session_id: string }>(
          `/api/v1/chat/sessions/${sessionId}/messages`,
          { content },
        );

        // Refetch so the persisted user message lands before we clear the
        // pending optimistic copy. Otherwise there's a flicker where neither
        // the optimistic nor the persisted message renders.
        await queryClient.invalidateQueries({ queryKey: ["chat-session", sessionId] });
        setPendingMessage(null);

        // Step 2: connect to the SSE stream for this run
        await connectToRunStream(run_id, sessionId);
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        if (err instanceof Error && err.message.includes("aborted")) return;
        const message =
          err instanceof Error
            ? err.message
            : "Failed to send message. Please try again.";
        setError(message);
        setPendingMessage(null);
      }
    },
    [activeSessionId, createSession, connectToRunStream, queryClient],
  );

  const handleNewChat = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    setActiveSessionId(null);
    setError(null);
  }, []);

  return {
    sessions,
    activeSessionId,
    setActiveSessionId,
    sessionDetail,
    isLoadingDetail,
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
