"use client";

import { useState, useCallback } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { ChatSession, ChatSessionDetail } from "@/lib/types";
import { SessionSidebar } from "@/components/chat/session-sidebar";
import { MessageList } from "@/components/chat/message-list";
import { ChatInput } from "@/components/chat/chat-input";

export default function ChatPage() {
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const { data: sessions = [] } = useQuery<ChatSession[]>({
    queryKey: ["chat-sessions"],
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

  const sendMessage = useMutation({
    mutationFn: ({ sessionId, content }: { sessionId: string; content: string }) =>
      apiClient.post(`/api/v1/chat/sessions/${sessionId}/messages`, { content }),
    onSuccess: () => {
      if (activeSessionId) {
        queryClient.invalidateQueries({ queryKey: ["chat-session", activeSessionId] });
        queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
      }
    },
  });

  const handleSend = useCallback(
    async (content: string) => {
      let sessionId = activeSessionId;
      if (!sessionId) {
        const session = await createSession.mutateAsync();
        sessionId = session.id;
      }
      await sendMessage.mutateAsync({ sessionId, content });
    },
    [activeSessionId, createSession, sendMessage],
  );

  const handleNewChat = useCallback(() => {
    setActiveSessionId(null);
  }, []);

  return (
    <div className="flex h-[calc(100vh-3rem)] gap-0 -m-6">
      <SessionSidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        onSelectSession={setActiveSessionId}
        onNewChat={handleNewChat}
      />
      <div className="flex flex-1 flex-col">
        <div className="flex-1 overflow-hidden">
          <MessageList
            messages={sessionDetail?.messages || []}
            isLoading={isLoadingDetail && !!activeSessionId}
          />
        </div>
        <ChatInput
          onSend={handleSend}
          isLoading={sendMessage.isPending || createSession.isPending}
        />
      </div>
    </div>
  );
}
