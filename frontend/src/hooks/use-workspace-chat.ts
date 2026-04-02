"use client";

import { useState, useCallback, useRef } from "react";
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

  const handleSend = useCallback(
    async (content: string) => {
      if (isStreaming || createSession.isPending) return;
      setError(null);
      setPendingMessage(content);
      setIsStreaming(true);
      setStreamBlocks([]);
      setStreamingMessage(null);

      let sessionId = activeSessionId;
      if (!sessionId) {
        try {
          const session = await createSession.mutateAsync();
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
        const message =
          err instanceof Error
            ? err.message
            : "Failed to send message. Please try again.";
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
        await queryClient.invalidateQueries({
          queryKey: ["chat-session", sessionId],
        });
        await queryClient.invalidateQueries({
          queryKey: ["chat-sessions"],
        });
        setIsStreaming(false);
        setPendingMessage(null);
        setStreamBlocks([]);
        setStreamingMessage(null);
      }
    },
    [activeSessionId, createSession, flushBuffer, isStreaming, queryClient],
  );

  const handleNewChat = useCallback(() => {
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
