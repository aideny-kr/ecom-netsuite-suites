"use client";

import { useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";
import type { ChatMessage } from "@/lib/types";
import { ToolCallStepCard } from "@/components/chat/tool-call-step";
import { Skeleton } from "@/components/ui/skeleton";

interface MessageListProps {
  messages: ChatMessage[];
  isLoading: boolean;
}

export function MessageList({ messages, isLoading }: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Skeleton className="h-8 w-48" />
      </div>
    );
  }

  if (messages.length === 0) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <h3 className="text-lg font-medium">How can I help?</h3>
          <p className="text-sm text-muted-foreground">
            Ask questions about your data, docs, or NetSuite operations.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto p-4 space-y-4">
      {messages.map((message) => (
        <div
          key={message.id}
          className={cn(
            "flex",
            message.role === "user" ? "justify-end" : "justify-start",
          )}
        >
          <div
            className={cn(
              "max-w-[80%] rounded-lg px-4 py-2",
              message.role === "user"
                ? "bg-primary text-primary-foreground"
                : "bg-muted",
            )}
          >
            {message.tool_calls && message.tool_calls.length > 0 && (
              <div className="mb-2 space-y-1">
                {message.tool_calls.map((tc, idx) => (
                  <ToolCallStepCard key={idx} step={tc} />
                ))}
              </div>
            )}
            {message.role === "assistant" ? (
              <div className="prose prose-sm dark:prose-invert max-w-none">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {message.content}
                </ReactMarkdown>
              </div>
            ) : (
              <p className="text-sm whitespace-pre-wrap">{message.content}</p>
            )}
            {message.citations && message.citations.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {message.citations.map((citation, idx) => (
                  <span
                    key={idx}
                    className="inline-flex items-center rounded-full bg-background/50 px-2 py-0.5 text-xs"
                    title={citation.snippet}
                  >
                    {citation.type === "doc" ? "\u{1F4C4}" : "\u{1F4CA}"} {citation.title}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
