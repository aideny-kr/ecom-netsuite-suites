"use client";

import { useState, useCallback, useMemo, type KeyboardEvent } from "react";
import { ArrowUp, AtSign, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { FileMentionPicker } from "@/components/chat/file-mention-picker";
import { AnalyticsDashboard } from "@/components/analytics/AnalyticsDashboard";

interface ChatInputProps {
  onSend: (content: string) => void;
  isLoading: boolean;
  workspaceId?: string | null;
}

export function ChatInput({ onSend, isLoading, workspaceId }: ChatInputProps) {
  const [value, setValue] = useState("");
  const [mentionOpen, setMentionOpen] = useState(false);
  const [commandOpen, setCommandOpen] = useState(false);
  const [analyticsOpen, setAnalyticsOpen] = useState(false);

  const mentions = useMemo(
    () => Array.from(value.matchAll(/@workspace:([^\s]+)/g)).map((m) => m[1]),
    [value],
  );

  const handleSend = useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed || isLoading) return;
    onSend(trimmed);
    setValue("");
  }, [value, isLoading, onSend]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  const handleMentionSelect = useCallback(
    (file: { file_id: string; path: string }) => {
      setValue((prev) => {
        // Remove trailing @ if present
        const cleaned = prev.endsWith("@") ? prev.slice(0, -1) : prev;
        return `${cleaned}@workspace:${file.path} `;
      });
    },
    [],
  );

  const handleRemoveMention = useCallback((filePath: string) => {
    setValue((prev) => prev.replace(`@workspace:${filePath} `, "").replace(`@workspace:${filePath}`, ""));
  }, []);

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      const newVal = e.target.value.slice(0, 4000);
      setValue(newVal);

      // Detect @ at end of input (or after space) to trigger mention picker
      if (
        workspaceId &&
        newVal.endsWith("@") &&
        (newVal.length === 1 || newVal[newVal.length - 2] === " ")
      ) {
        setMentionOpen(true);
      }

      // Detect / at the start of input to trigger command picker
      if (newVal === "/") {
        setCommandOpen(true);
      } else if (!newVal.startsWith("/")) {
        setCommandOpen(false);
      }
    },
    [workspaceId],
  );

  return (
    <div className="border-t bg-card px-6 py-4">
      <div className="mx-auto max-w-3xl">
        {/* Attachment chips */}
        {mentions.length > 0 && (
          <div className="mb-1.5 flex flex-wrap gap-1">
            {mentions.map((path) => (
              <span
                key={path}
                className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary"
              >
                {path.split("/").pop()}
                <button
                  onClick={() => handleRemoveMention(path)}
                  className="hover:text-destructive"
                  title={`Remove ${path}`}
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
          </div>
        )}
        <div className="relative flex items-end gap-3 rounded-2xl border bg-background p-2 shadow-soft transition-shadow focus-within:shadow-soft-md focus-within:ring-1 focus-within:ring-ring">
          <textarea
            value={value}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            placeholder={
              workspaceId
                ? "Ask a question... (type @ to reference a file)"
                : "Ask a question..."
            }
            disabled={isLoading}
            rows={1}
            className="flex-1 resize-none bg-transparent px-2 py-1.5 text-[14px] placeholder:text-muted-foreground focus-visible:outline-none disabled:opacity-50"
            style={{ minHeight: "2rem", maxHeight: "8rem" }}
            onInput={(e) => {
              const target = e.target as HTMLTextAreaElement;
              target.style.height = "auto";
              target.style.height = `${Math.min(target.scrollHeight, 128)}px`;
            }}
          />
          {workspaceId && (
            <FileMentionPicker
              open={mentionOpen}
              onOpenChange={setMentionOpen}
              workspaceId={workspaceId}
              onSelect={handleMentionSelect}
            >
              <Button
                size="icon"
                variant="ghost"
                className="h-8 w-8 shrink-0 rounded-xl"
                onClick={() => setMentionOpen(!mentionOpen)}
                aria-label="Mention file"
                title="Reference a workspace file"
              >
                <AtSign className="h-4 w-4" />
              </Button>
            </FileMentionPicker>
          )}
          <Button
            size="icon"
            className="h-8 w-8 shrink-0 rounded-xl"
            onClick={handleSend}
            disabled={!value.trim() || isLoading}
            aria-label="Send message"
            title="Send message"
          >
            <ArrowUp className="h-4 w-4" />
          </Button>
          {commandOpen && (
            <div className="absolute bottom-full mb-2 left-2 z-50 w-64 rounded-xl border bg-card p-1 shadow-lg overflow-hidden">
              <div className="px-2 py-1.5 text-xs font-semibold text-muted-foreground">Commands</div>
              <button
                className="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-sm text-foreground hover:bg-primary hover:text-primary-foreground transition-colors"
                onClick={() => {
                  setCommandOpen(false);
                  setValue("");
                  setAnalyticsOpen(true);
                }}
              >
                <span className="font-mono text-xs">/export_analytics</span>
                <span className="text-muted-foreground">View Saved Queries</span>
              </button>
            </div>
          )}
        </div>
        <p className="mt-1.5 text-right text-[11px] tabular-nums text-muted-foreground">
          {value.length}/4000
        </p>
      </div>

      <AnalyticsDashboard open={analyticsOpen} onOpenChange={setAnalyticsOpen} />
    </div>
  );
}
