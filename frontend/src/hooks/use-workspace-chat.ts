"use client";

import { useState, useCallback } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type {
  ChatSession,
  ChatSessionDetail,
  ChatMessage,
} from "@/lib/types";

export function useWorkspaceChat(workspaceId: string | null) {
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [pendingMessage, setPendingMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const { data: sessions = [] } = useQuery<ChatSession[]>({
    queryKey: ["workspace-chat-sessions", workspaceId],
    queryFn: () =>
      apiClient.get<ChatSession[]>(
        `/api/v1/chat/sessions?workspace_id=${workspaceId}`,
      ),
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
      refetchInterval: (query) => {
        const data = query.state.data;
        if (!data) return false;
        const msgs = data.messages;
        // Refetch while waiting for assistant reply
        if (msgs.length > 0 && msgs[msgs.length - 1].role === "user") {
          return 2000;
        }
        return false;
      },
    });

  const createSession = useMutation({
    mutationFn: () =>
      apiClient.post<ChatSession>("/api/v1/chat/sessions", {
        workspace_id: workspaceId,
      }),
    onSuccess: (session) => {
      queryClient.invalidateQueries({
        queryKey: ["workspace-chat-sessions", workspaceId],
      });
      setActiveSessionId(session.id);
    },
  });

  const sendMessage = useMutation({
    mutationFn: ({
      sessionId,
      content,
    }: {
      sessionId: string;
      content: string;
    }) =>
      apiClient.post<ChatMessage>(
        `/api/v1/chat/sessions/${sessionId}/messages`,
        { content },
      ),
    onSuccess: () => {
      setPendingMessage(null);
      if (activeSessionId) {
        queryClient.invalidateQueries({
          queryKey: ["chat-session", activeSessionId],
        });
        queryClient.invalidateQueries({
          queryKey: ["workspace-chat-sessions", workspaceId],
        });
      }
    },
    onError: (err: Error) => {
      setPendingMessage(null);
      setError(err.message || "Failed to send message.");
    },
  });

  const handleSend = useCallback(
    async (content: string) => {
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
      sendMessage.mutate({ sessionId, content });
    },
    [activeSessionId, createSession, sendMessage],
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
    isSending: sendMessage.isPending || createSession.isPending,
  };
}
