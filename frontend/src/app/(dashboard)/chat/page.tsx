"use client";

import { useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
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
  const [isStreaming, setIsStreaming] = useState(false);
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
    <div className="flex h-[calc(100vh-4rem)] -mx-8 -my-8 animate-fade-in">
      <SessionSidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        onSelectSession={setActiveSessionId}
        onNewChat={handleNewChat}
      />
      <div className="flex flex-1 flex-col bg-card">
        <div className="flex-1 overflow-hidden">
          <MessageList
            messages={sessionDetail?.messages || []}
            isLoading={isLoadingDetail && !!activeSessionId}
            pendingUserMessage={pendingMessage}
            isWaitingForReply={isStreaming}
            streamingContent={streamingContent}
            streamingStatus={streamingStatus}
            onMentionClick={handleMentionClick}
          />
        </div>
        {error && (
          <div className="mx-6 mb-2 flex items-center gap-2 rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-2.5 text-[13px] text-destructive">
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
          onSend={handleSend}
          isLoading={isStreaming || createSession.isPending}
          workspaceId={workspaces[0]?.id || null}
        />
      </div>
    </div>
  );
}
