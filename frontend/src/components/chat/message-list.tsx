"use client";

import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { useCreateSavedQuery } from "@/hooks/use-saved-queries";
import { cn } from "@/lib/utils";
import type { ChatMessage } from "@/lib/types";
import { ToolCallStepCard } from "@/components/chat/tool-call-step";
import { ChangeProposalCard } from "@/components/chat/change-proposal-card";
import { WorkspaceToolCard } from "@/components/chat/workspace-tool-card";
import { SuiteQLToolCard } from "@/components/chat/suiteql-tool-card";
import { FileCode, Bookmark, Check, Loader2, Copy } from "lucide-react";

/** Framework-inspired gear/module icon used as AI assistant avatar.
 *  A square with notches on each side — resembles the Framework Computer logo. */
function FrameworkIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className}>
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M8 1a1 1 0 0 0-1 1v2H4a2 2 0 0 0-2 2v1a1 1 0 0 0 1 1h2v8H3a1 1 0 0 0-1 1v1a2 2 0 0 0 2 2h3v2a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1v-2h2v2a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1v-2h3a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1h-2V8h2a1 1 0 0 0 1-1V6a2 2 0 0 0-2-2h-3V2a1 1 0 0 0-1-1h-2a1 1 0 0 0-1 1v2h-2V2a1 1 0 0 0-1-1H8zm1 7a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V9a1 1 0 0 0-1-1H9z"
      />
    </svg>
  );
}

/** Shared markdown components with syntax-highlighted code blocks */
const mdComponents: Components = {
  code({ className, children, ...props }) {
    const match = /language-(\w+)/.exec(className || "");
    const codeString = String(children).replace(/\n$/, "");

    if (!match) {
      return (
        <code
          className="rounded bg-muted px-1.5 py-0.5 text-[13px] font-mono text-foreground"
          {...props}
        >
          {children}
        </code>
      );
    }

    return (
      <div className="group relative my-3 rounded-lg overflow-hidden border border-border/50">
        <div className="flex items-center justify-between bg-muted/80 px-3 py-1.5 text-[11px] font-medium text-muted-foreground">
          <span>{match[1]}</span>
          <button
            onClick={() => navigator.clipboard.writeText(codeString)}
            className="opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-1 hover:text-foreground"
          >
            <Copy className="h-3 w-3" />
            Copy
          </button>
        </div>
        <SyntaxHighlighter
          style={oneDark}
          language={match[1]}
          PreTag="div"
          customStyle={{
            margin: 0,
            borderRadius: 0,
            fontSize: "13px",
            lineHeight: "1.5",
          }}
        >
          {codeString}
        </SyntaxHighlighter>
      </div>
    );
  },
};

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

/** Shared pattern for matching closed thinking/reasoning XML blocks. Must create new RegExp for each use (stateful /g flag). */
const THINKING_TAG_PATTERN = String.raw`<(?:thinking|reasoning)>([\s\S]*?)<\/(?:thinking|reasoning)>`;

function parseThinkingBlocks(content: string): Array<{
  type: "text" | "thinking";
  content: string;
}> {
  const parts: Array<{ type: "text" | "thinking"; content: string }> = [];
  const regex = new RegExp(THINKING_TAG_PATTERN, "g");
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

/**
 * Parse streaming content to separate thinking/reasoning blocks from text.
 * Handles incomplete (still-open) tags during streaming.
 */
function parseStreamingThinking(content: string): {
  thinking: string | null;
  isThinking: boolean;
  text: string;
} {
  const closedRegex = new RegExp(THINKING_TAG_PATTERN, "g");
  let lastThinking: string | null = null;
  let lastIndex = 0;
  let match;
  const textParts: string[] = [];

  while ((match = closedRegex.exec(content)) !== null) {
    if (match.index > lastIndex) {
      const text = content.slice(lastIndex, match.index).trim();
      if (text) textParts.push(text);
    }
    lastThinking = match[1].trim();
    lastIndex = closedRegex.lastIndex;
  }

  // Check remainder after all closed tags for an unclosed open tag
  const remainder = content.slice(lastIndex);
  const openTagMatch = remainder.match(/<(thinking|reasoning)>([\s\S]*)$/);

  if (openTagMatch) {
    const beforeOpenTag = remainder.slice(0, openTagMatch.index).trim();
    if (beforeOpenTag) textParts.push(beforeOpenTag);
    return {
      thinking: openTagMatch[2].trim() || null,
      isThinking: true,
      text: textParts.join("\n\n"),
    };
  }

  if (lastIndex > 0) {
    const remainingText = remainder.trim();
    if (remainingText) textParts.push(remainingText);
    return {
      thinking: lastThinking,
      isThinking: false,
      text: textParts.join("\n\n"),
    };
  }

  return { thinking: null, isThinking: false, text: content };
}

/** Collapsed thinking block for completed messages */
function ThinkingBlock({ content }: { content: string }) {
  return (
    <details className="mb-2 rounded-lg border border-muted/50 bg-muted/20 text-[12px] group">
      <summary className="cursor-pointer select-none px-3 py-2 text-muted-foreground/60 hover:text-muted-foreground font-medium flex items-center gap-2 transition-colors">
        <FrameworkIcon className="h-3 w-3" />
        Thought process
      </summary>
      <div className="prose prose-sm dark:prose-invert max-w-none px-3 pb-2.5 text-muted-foreground/70 text-[12px] leading-relaxed">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>{content}</ReactMarkdown>
      </div>
    </details>
  );
}

/** Live thinking block shown during streaming — Gemini-style animation */
function StreamingThinkingBlock({ content, isActive }: { content: string | null; isActive: boolean }) {
  return (
    <div className="mb-2 rounded-lg border border-primary/10 bg-primary/[0.03] overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2">
        {isActive && (
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary/40" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-primary/60" />
          </span>
        )}
        <span className="text-[12px] font-medium text-primary/70">
          {isActive ? "Thinking..." : "Thought process"}
        </span>
      </div>
      {content && (
        <div className={cn(
          "px-3 pb-2.5 text-[12px] leading-relaxed text-muted-foreground/60",
          isActive && "animate-thinking-fade"
        )}>
          {/* Use plain text while actively streaming to avoid expensive ReactMarkdown re-renders per chunk */}
          {isActive ? (
            <p className="whitespace-pre-wrap">{content}</p>
          ) : (
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>{content}</ReactMarkdown>
          )}
        </div>
      )}
    </div>
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

  // Use instant scroll during streaming (smooth can't keep up with rapid updates),
  // smooth scroll for message list changes (new messages loaded, pending message shown).
  const isStreamingNow = !!(streamingContent || isWaitingForReply);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: isStreamingNow ? "auto" : "smooth" });
  }, [messages, pendingUserMessage, isWaitingForReply, streamingContent, isStreamingNow]);

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
            <FrameworkIcon className="h-6 w-6 text-primary" />
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
              <FrameworkIcon className="h-3.5 w-3.5 text-primary" />
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
                  if (tc.tool === "netsuite_suiteql") {
                    const msgIndex = messages.indexOf(message);
                    const prevUserMsg = messages
                      .slice(0, msgIndex)
                      .reverse()
                      .find((m) => m.role === "user");
                    return (
                      <SuiteQLToolCard
                        key={idx}
                        step={tc}
                        userQuestion={prevUserMsg?.content}
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
                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
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
            {message.role === "assistant" &&
              message.tool_calls?.some((tc) => tc.tool === "netsuite_suiteql") && (
              <InlineSaveLink
                message={message}
                messages={messages}
              />
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
            <FrameworkIcon className="h-3.5 w-3.5 text-primary" />
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

            {streamingContent && (() => {
              const parsed = parseStreamingThinking(streamingContent);
              return (
                <>
                  {(parsed.thinking !== null || parsed.isThinking) && (
                    <StreamingThinkingBlock
                      content={parsed.thinking}
                      isActive={parsed.isThinking}
                    />
                  )}
                  {parsed.text && (
                    <div className="prose prose-sm dark:prose-invert max-w-none text-[14px] leading-relaxed overflow-x-auto">
                      <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                        {parsed.text}
                      </ReactMarkdown>
                    </div>
                  )}
                </>
              );
            })()}
          </div>
        </div>
      )}

      <div ref={bottomRef} />
    </div>
  );
}

/**
 * Subtle inline save link rendered below assistant messages that contain
 * SuiteQL tool calls. Provides a secondary save trigger near the data table.
 */
function InlineSaveLink({
  message,
  messages,
}: {
  message: ChatMessage;
  messages: ChatMessage[];
}) {
  const [state, setState] = useState<"idle" | "saving" | "saved">("idle");

  const suiteqlCall = message.tool_calls?.find(
    (tc) => tc.tool === "netsuite_suiteql",
  );
  const queryText = (suiteqlCall?.params?.query as string) ?? "";

  const msgIndex = messages.indexOf(message);
  const prevUserMsg = messages
    .slice(0, msgIndex)
    .reverse()
    .find((m) => m.role === "user");
  const autoName = prevUserMsg?.content?.slice(0, 120) ?? "Saved Query";

  const mutation = useCreateSavedQuery();

  if (!queryText) return null;

  if (state === "saved") {
    return (
      <div className="mt-2 flex items-center gap-1.5 text-[11px] font-medium text-green-600 dark:text-green-400">
        <Check className="h-3 w-3" />
        Saved to Analytics
      </div>
    );
  }

  return (
    <div className="mt-2">
      <button
        onClick={() => {
          setState("saving");
          mutation.mutate(
            { name: autoName, query_text: queryText },
            { onSuccess: () => setState("saved") },
          );
        }}
        disabled={mutation.isPending}
        className="flex items-center gap-1.5 text-[11px] text-muted-foreground hover:text-primary transition-colors"
      >
        {mutation.isPending ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : (
          <Bookmark className="h-3 w-3" />
        )}
        Save query to Analytics
      </button>
      {mutation.isError && (
        <span className="mt-0.5 text-[11px] text-destructive">
          Failed to save
        </span>
      )}
    </div>
  );
}
