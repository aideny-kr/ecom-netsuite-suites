"use client";

import { useCallback, useRef, useState } from "react";
import {
  MessageCircle,
  Paperclip,
  Plus,
  AlertCircle,
  FileCode,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { MessageList } from "@/components/chat/message-list";
import { ChatInput } from "@/components/chat/chat-input";
import { useWorkspaceChat, type WorkspaceSendOptions } from "@/hooks/use-workspace-chat";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { useWebMcpState, useWebMcpTools } from "@/hooks/use-webmcp-tools";
import { createChatTools, type WebMcpChatState } from "@/lib/webmcp-chat";
import { workspaceChatContent } from "@/lib/workspace-chat-context";

interface WorkspaceChatPanelProps {
  workspaceId: string;
  currentFilePath?: string;
  onMentionClick?: (filePath: string) => void;
  onViewDiff?: (changesetId: string) => void;
  onChangesetAction?: () => void;
}

export function WorkspaceChatPanel({
  workspaceId,
  currentFilePath,
  onMentionClick,
  onViewDiff,
  onChangesetAction,
}: WorkspaceChatPanelProps) {
  const {
    sessions,
    activeSessionId,
    setActiveSessionId,
    sessionDetail,
    isLoadingDetail,
    isLoadingSessions,
    createConversation,
    cancelActiveRun,
    handleStop,
    activeRunId,
    pendingMessage,
    error,
    setError,
    handleSend,
    handleNewChat,
    isSending,
    streamBlocks,
    streamingMessage,
  } = useWorkspaceChat(workspaceId);

  const { data: health } = useQuery<{ max_input_chars: number }>({
    queryKey: ["chat-health"],
    queryFn: () => apiClient.get("/api/v1/chat/health"),
    staleTime: 60_000,
  });
  // The same file-context and attachment path serves both human and agent sends.
  const handleSendWithContext = useCallback(
    async (content: string, fileId?: string, opts: WorkspaceSendOptions = {}) => {
      try {
        opts.assert_current?.();
        const enriched = workspaceChatContent(content, currentFilePath, health?.max_input_chars || 32000);
        return await handleSend(enriched, fileId, opts);
      } catch (error) {
        if (opts.request_id) throw error;
        setError(error instanceof Error ? error.message : "Failed to send message.");
      }
    },
    [handleSend, currentFilePath, health?.max_input_chars, setError],
  );
  const chatState = useWebMcpState<WebMcpChatState>({
    sessionId: activeSessionId, sessions, detail: sessionDetail,
    workspaceId, filePath: currentFilePath || null, activeRunId,
    busy: isSending, loading: isLoadingSessions || (!!activeSessionId && sessionDetail?.id !== activeSessionId),
    hasError: !!error, create: createConversation, select: setActiveSessionId,
    send: (content, requestId, assertCurrent) => handleSendWithContext(content, undefined, { request_id: requestId, assert_current: assertCurrent }),
    cancel: cancelActiveRun,
  });
  useWebMcpTools(`workspace-chat:${workspaceId}`, createChatTools(chatState, true));

  const inputRef = useRef<{ insertText: (text: string) => void }>(null);
  const [attachedHint, setAttachedHint] = useState<string | null>(null);

  const handleAttachFile = useCallback(() => {
    if (!currentFilePath) return;
    const mention = `@workspace:${currentFilePath} `;
    setAttachedHint(currentFilePath);
    // Try to insert into input; fall back to sending a message about the file
    if (inputRef.current) {
      inputRef.current.insertText(mention);
    }
    setTimeout(() => setAttachedHint(null), 2000);
  }, [currentFilePath]);

  return (
    <div
      className="flex h-full min-h-0 min-w-0 flex-col"
      data-testid="workspace-chat-panel"
    >
      {/* Header */}
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <MessageCircle className="h-3.5 w-3.5 text-muted-foreground" />
        <span className="text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
          Chat
        </span>
        <div className="ml-auto flex items-center gap-1">
          {currentFilePath && (
            <Button
              size="icon"
              variant="ghost"
              className="h-6 w-6"
              onClick={handleAttachFile}
              title={`Attach ${currentFilePath}`}
            >
              <Paperclip className="h-3 w-3" />
            </Button>
          )}
          <Button
            size="icon"
            variant="ghost"
            className="h-6 w-6"
            onClick={handleNewChat}
            title="New chat"
          >
            <Plus className="h-3 w-3" />
          </Button>
        </div>
      </div>

      {/* Session tabs */}
      {sessions.length > 1 && (
        <div className="flex gap-1 overflow-x-auto border-b px-2 py-1 scrollbar-thin">
          {sessions.slice(0, 5).map((s) => (
            <button
              key={s.id}
              onClick={() => setActiveSessionId(s.id)}
              className={`shrink-0 rounded px-2 py-0.5 text-[11px] ${
                activeSessionId === s.id
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-accent"
              }`}
            >
              {s.title || "New chat"}
            </button>
          ))}
        </div>
      )}

      {/* Attach hint */}
      {attachedHint && (
        <div className="mx-2 mt-1 rounded bg-primary/10 px-2 py-1 text-[11px] text-primary">
          Attached: {attachedHint}
        </div>
      )}

      {/* Messages */}
      <div className="min-h-0 min-w-0 flex-1 overflow-hidden">
        <MessageList
          messages={sessionDetail?.messages || []}
          isLoading={isLoadingDetail && !!activeSessionId}
          pendingUserMessage={pendingMessage}
          isWaitingForReply={isSending}
          streamBlocks={streamBlocks}
          streamingMessage={streamingMessage}
          onMentionClick={onMentionClick}
          workspaceId={workspaceId}
          onViewDiff={onViewDiff}
          onChangesetAction={onChangesetAction}
        />
      </div>

      {/* Error */}
      {error && (
        <div className="mx-2 mb-1 flex items-center gap-1.5 rounded-md border border-destructive/50 bg-destructive/10 px-2 py-1.5 text-[12px] text-destructive">
          <AlertCircle className="h-3 w-3 shrink-0" />
          <span className="flex-1 truncate">{error}</span>
          <button onClick={() => setError(null)}>
            <X className="h-3 w-3" />
          </button>
        </div>
      )}

      {/* Current file indicator */}
      {currentFilePath && (
        <div className="border-t bg-muted/30 px-3 py-1 text-[11px] text-muted-foreground flex items-center gap-1">
          <FileCode className="h-3 w-3" />
          <span className="truncate">Viewing: {currentFilePath}</span>
        </div>
      )}

      {/* Input */}
      <div className="border-t">
        <ChatInput
          onSend={handleSendWithContext}
          onStop={handleStop}
          isRunning={!!activeRunId}
          reservedChars={currentFilePath ? `[Currently viewing file: ${currentFilePath}]\n\n`.length : 0}
          isLoading={isSending}
          workspaceId={workspaceId}
        />
      </div>
    </div>
  );
}
