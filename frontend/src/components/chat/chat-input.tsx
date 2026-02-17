"use client";

import { useState, useCallback, type KeyboardEvent } from "react";
import { ArrowUp, AtSign } from "lucide-react";
import { Button } from "@/components/ui/button";
import { FileMentionPicker } from "@/components/chat/file-mention-picker";

interface ChatInputProps {
  onSend: (content: string) => void;
  isLoading: boolean;
  workspaceId?: string | null;
}

export function ChatInput({ onSend, isLoading, workspaceId }: ChatInputProps) {
  const [value, setValue] = useState("");
  const [mentionOpen, setMentionOpen] = useState(false);

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
    },
    [workspaceId],
  );

  return (
    <div className="border-t bg-card px-6 py-4">
      <div className="mx-auto max-w-3xl">
        <div className="flex items-end gap-3 rounded-2xl border bg-background p-2 shadow-soft transition-shadow focus-within:shadow-soft-md focus-within:ring-1 focus-within:ring-ring">
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
        </div>
        <p className="mt-1.5 text-right text-[11px] tabular-nums text-muted-foreground">
          {value.length}/4000
        </p>
      </div>
    </div>
  );
}
