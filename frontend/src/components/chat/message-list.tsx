"use client";

import { useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";
import type { ChatMessage } from "@/lib/types";
import { ToolCallStepCard } from "@/components/chat/tool-call-step";
import { ChangeProposalCard } from "@/components/chat/change-proposal-card";
import { WorkspaceToolCard } from "@/components/chat/workspace-tool-card";
import { Sparkles, FileCode } from "lucide-react";

function renderWithMentions(
  content: string,
  onMentionClick?: (filePath: string) => void,
): React.ReactNode[] {
  const mentionRegex = /@workspace:([^\s]+)/g;
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let match;

  while ((match = mentionRegex.exec(content)) !== null) {
    if (match.index > lastIndex) {
      parts.push(content.slice(lastIndex, match.index));
    }
    const filePath = match[1];
    parts.push(
      <button
        key={match.index}
        onClick={() => onMentionClick?.(filePath)}
        className="inline-flex items-center gap-1 rounded bg-primary/10 px-1.5 py-0.5 text-[12px] font-medium text-primary hover:bg-primary/20 cursor-pointer transition-colors"
        title={`Open ${filePath} in workspace`}
      >
        <FileCode className="h-3 w-3" />
        {filePath.split("/").pop()}
      </button>,
    );
    lastIndex = mentionRegex.lastIndex;
  }

  if (lastIndex < content.length) {
    parts.push(content.slice(lastIndex));
  }

  return parts.length > 0 ? parts : [content];
}

function parseThinkingBlocks(content: string): Array<{
  type: "text" | "thinking";
  content: string;
}> {
  const parts: Array<{ type: "text" | "thinking"; content: string }> = [];
  const regex = /<thinking>([\s\S]*?)<\/thinking>/g;
  let lastIndex = 0;
  let match;

  while ((match = regex.exec(content)) !== null) {
    if (match.index > lastIndex) {
      const text = content.slice(lastIndex, match.index).trim();
      if (text) parts.push({ type: "text", content: text });
    }
    parts.push({ type: "thinking", content: match[1].trim() });
    lastIndex = regex.lastIndex;
  }

  if (lastIndex < content.length) {
    const text = content.slice(lastIndex).trim();
    if (text) parts.push({ type: "text", content: text });
  }

  return parts.length > 0 ? parts : [{ type: "text", content }];
}

function ThinkingBlock({ content }: { content: string }) {
  return (
    <details className="mb-2 rounded-md border border-muted bg-muted/30 text-[12px]">
      <summary className="cursor-pointer select-none px-3 py-1.5 text-muted-foreground/70 hover:text-muted-foreground font-medium">
        Thinking...
      </summary>
      <div className="prose prose-sm dark:prose-invert max-w-none px-3 pb-2 text-muted-foreground/80 text-[12px]">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
      </div>
    </details>
  );
}

interface MessageListProps {
  messages: ChatMessage[];
  isLoading: boolean;
  pendingUserMessage?: string | null;
  isWaitingForReply?: boolean;
  onMentionClick?: (filePath: string) => void;
  workspaceId?: string | null;
  onViewDiff?: (changesetId: string) => void;
  onChangesetAction?: () => void;
  streamingContent?: string | null;
  streamingStatus?: string | null;
}

export function MessageList({
  messages,
  isLoading,
  pendingUserMessage,
  isWaitingForReply,
  onMentionClick,
  workspaceId,
  onViewDiff,
  onChangesetAction,
  streamingContent,
  streamingStatus,
}: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, pendingUserMessage, isWaitingForReply]);

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          <span className="text-[13px] text-muted-foreground">
            Loading conversation...
          </span>
        </div>
      </div>
    );
  }

  if (messages.length === 0 && !pendingUserMessage) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-primary/10">
            <Sparkles className="h-6 w-6 text-primary" />
          </div>
          <h3 className="text-lg font-semibold text-foreground">
            How can I help?
          </h3>
          <p className="mt-1.5 max-w-xs text-[14px] leading-relaxed text-muted-foreground">
            Ask questions about your data, docs, or NetSuite operations.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto px-6 py-6 space-y-5 scrollbar-thin">
      {messages.map((message) => (
        <div
          key={message.id}
          className={cn(
            "flex gap-3",
            message.role === "user" ? "justify-end" : "justify-start",
          )}
        >
          {message.role === "assistant" && (
            <div className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary/10">
              <Sparkles className="h-3.5 w-3.5 text-primary" />
            </div>
          )}
          <div
            className={cn(
              "max-w-[75%] rounded-2xl px-4 py-2.5",
              message.role === "user"
                ? "bg-primary text-primary-foreground"
                : "bg-muted/60",
            )}
          >
            {message.tool_calls && message.tool_calls.length > 0 && (
              <div className="mb-2 space-y-1.5">
                {message.tool_calls.map((tc, idx) => {
                  if (
                    tc.tool === "workspace_propose_patch" &&
                    workspaceId &&
                    onViewDiff
                  ) {
                    return (
                      <ChangeProposalCard
                        key={idx}
                        step={tc}
                        workspaceId={workspaceId}
                        onViewDiff={onViewDiff}
                        onChangesetAction={onChangesetAction}
                      />
                    );
                  }
                  if (tc.tool.startsWith("workspace_")) {
                    return <WorkspaceToolCard key={idx} step={tc} />;
                  }
                  return <ToolCallStepCard key={idx} step={tc} />;
                })}
              </div>
            )}
            {message.role === "assistant" ? (() => {
              const parts = parseThinkingBlocks(message.content);
              return (
                <div>
                  {parts.map((part, i) =>
                    part.type === "thinking" ? (
                      <ThinkingBlock key={i} content={part.content} />
                    ) : (
                      <div key={i} className="prose prose-sm dark:prose-invert max-w-none text-[14px] leading-relaxed overflow-x-auto">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {part.content}
                        </ReactMarkdown>
                      </div>
                    )
                  )}
                </div>
              );
            })() : (
              <p className="text-[14px] leading-relaxed whitespace-pre-wrap">
                {renderWithMentions(message.content, onMentionClick)}
              </p>
            )}
            {message.citations && message.citations.length > 0 && (
              <div className="mt-2.5 flex flex-wrap gap-1.5">
                {message.citations.map((citation, idx) => (
                  <span
                    key={idx}
                    className="inline-flex items-center rounded-full bg-background/60 px-2.5 py-1 text-[11px] font-medium"
                    title={citation.snippet}
                  >
                    {citation.type === "doc" ? "\u{1F4C4}" : "\u{1F4CA}"} {citation.title}
                  </span>
                ))}
              </div>
            )}
            {message.role === "assistant" && message.model_used && (
              <div className="mt-1.5 flex items-center gap-1.5 text-[11px] text-muted-foreground/60">
                {message.is_byok ? (
                  <span className="rounded bg-blue-500/10 px-1.5 py-0.5 font-medium text-blue-600 dark:text-blue-400">
                    BYOK
                  </span>
                ) : (
                  <span className="rounded bg-muted px-1.5 py-0.5 font-medium">
                    Platform
                  </span>
                )}
                <span>{message.provider_used}</span>
                <span>/</span>
                <span>{message.model_used}</span>
                {message.input_tokens != null && message.output_tokens != null && (
                  <>
                    <span className="ml-1">·</span>
                    <span>{(message.input_tokens + message.output_tokens).toLocaleString()} tokens</span>
                  </>
                )}
              </div>
            )}
          </div>
        </div>
      ))}

      {/* Optimistic pending user message */}
      {pendingUserMessage && (
        <div className="flex gap-3 justify-end">
          <div className="max-w-[75%] rounded-2xl px-4 py-2.5 bg-primary text-primary-foreground">
            <p className="text-[14px] leading-relaxed whitespace-pre-wrap">
              {pendingUserMessage}
            </p>
          </div>
        </div>
      )}

      {/* Thinking indicator / Streaming */}
      {(isWaitingForReply || streamingContent || streamingStatus) && (
        <div className="flex gap-3 justify-start">
          <div className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary/10">
            <Sparkles className="h-3.5 w-3.5 text-primary" />
          </div>
          <div className="flex flex-col gap-2 rounded-2xl bg-muted/60 px-4 py-3 min-w-[30%]">
            {streamingStatus ? (
              <div className="text-[12px] font-medium text-muted-foreground flex items-center gap-2">
                <span className="h-1.5 w-1.5 rounded-full bg-primary animate-pulse" />
                {streamingStatus}
              </div>
            ) : !streamingContent ? (
              <span className="inline-flex gap-1 h-[20px] items-center">
                <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/60 animate-bounce [animation-delay:0ms]" />
                <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/60 animate-bounce [animation-delay:150ms]" />
                <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/60 animate-bounce [animation-delay:300ms]" />
              </span>
            ) : null}

            {streamingContent && (
              <div className="prose prose-sm dark:prose-invert max-w-none text-[14px] leading-relaxed overflow-x-auto">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {streamingContent}
                </ReactMarkdown>
              </div>
            )}
          </div>
        </div>
      )}

      <div ref={bottomRef} />
    </div>
  );
}
