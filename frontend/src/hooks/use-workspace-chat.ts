"use client";

import { useState, useCallback } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { ChatSession, ChatSessionDetail } from "@/lib/types";

export function useWorkspaceChat(workspaceId: string | null) {
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [pendingMessage, setPendingMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingContent, setStreamingContent] = useState<string | null>(null);
  const [streamingStatus, setStreamingStatus] = useState<string | null>(null);
  const queryClient = useQueryClient();

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
      setStreamingContent("");
      setStreamingStatus(null);

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

        const reader = res.body?.getReader();
        const decoder = new TextDecoder();
        if (!reader) throw new Error("Stream not available");

        let buffer = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          const chunks = buffer.split("\n\n");
          buffer = chunks.pop() || "";

          for (const chunk of chunks) {
            const lines = chunk.split("\n");
            for (const line of lines) {
              if (line.startsWith("data: ")) {
                const dataStr = line.slice(6).trim();
                if (!dataStr) continue;
                try {
                  const data = JSON.parse(dataStr);
                  if (data.type === "text") {
                    setStreamingContent((prev) => (prev || "") + data.content);
                    setStreamingStatus(null);
                  } else if (data.type === "tool_status") {
                    setStreamingStatus(data.content);
                  } else if (data.type === "error") {
                    setError(data.error);
                  }
                } catch (e) {
                  console.error("Failed to parse SSE line", e);
                }
              }
            }
          }
        }
      } catch (err: unknown) {
        const message =
          err instanceof Error
            ? err.message
            : "Failed to send message. Please try again.";
        setError(message);
      } finally {
        await queryClient.invalidateQueries({
          queryKey: ["chat-session", sessionId],
        });
        await queryClient.invalidateQueries({
          queryKey: ["chat-sessions"],
        });
        setIsStreaming(false);
        setPendingMessage(null);
        setStreamingContent(null);
        setStreamingStatus(null);
      }
    },
    [activeSessionId, createSession, isStreaming, queryClient],
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
    streamingContent,
    streamingStatus,
  };
}
