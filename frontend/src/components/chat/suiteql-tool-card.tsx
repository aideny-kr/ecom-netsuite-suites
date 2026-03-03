"use client";

import { useState } from "react";
import { useCreateSavedQuery } from "@/hooks/use-saved-queries";
import type { ToolCallStep } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  ChevronDown,
  Database,
  Bookmark,
  Check,
  Loader2,
  X,
  Pencil,
} from "lucide-react";

interface SuiteQLToolCardProps {
  step: ToolCallStep;
  userQuestion?: string;
}

export function SuiteQLToolCard({ step, userQuestion }: SuiteQLToolCardProps) {
  const [open, setOpen] = useState(false);
  const [saveMode, setSaveMode] = useState<"idle" | "editing" | "saved">("idle");
  const [name, setName] = useState(userQuestion?.slice(0, 120) ?? "");

  const queryText = (step.params?.query as string) ?? "";

  const mutation = useCreateSavedQuery();

  const handleMutationSuccess = () => setSaveMode("saved");

  const handleSave = () => {
    if (!name.trim() || !queryText.trim()) return;
    mutation.mutate(
      { name: name.trim(), query_text: queryText.trim() },
      { onSuccess: handleMutationSuccess },
    );
  };

  // Parse row count from result_summary if available
  const rowCountMatch = step.result_summary?.match(/(\d+)\s*rows?/i);
  const rowCount = rowCountMatch ? rowCountMatch[1] : null;

  return (
    <div className="rounded-lg border bg-background/80 text-[12px]">
      {/* Header */}
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2 transition-colors hover:bg-accent/50"
      >
        <ChevronDown
          className={cn(
            "h-3 w-3 shrink-0 text-muted-foreground transition-transform duration-200",
            !open && "-rotate-90",
          )}
        />
        <Database className="h-3 w-3 shrink-0 text-primary/70" />
        <span className="font-medium truncate">SuiteQL Query</span>
        {rowCount && (
          <span className="shrink-0 rounded-md bg-primary/10 px-1.5 py-0.5 text-[11px] tabular-nums text-primary font-medium">
            {rowCount} rows
          </span>
        )}
        <span className="ml-auto shrink-0 rounded-md bg-muted px-1.5 py-0.5 text-[11px] tabular-nums text-muted-foreground">
          {step.duration_ms}ms
        </span>
      </button>

      {/* Expanded body */}
      <div
        className={cn(
          "overflow-hidden transition-all duration-200",
          open ? "max-h-[600px]" : "max-h-0",
        )}
      >
        <div className="border-t px-3 py-2 space-y-2">
          {/* Formatted SQL */}
          <pre className="whitespace-pre-wrap break-all rounded-md bg-muted/50 px-2.5 py-2 text-[11px] font-mono text-foreground/90 leading-relaxed overflow-x-auto max-h-[200px] scrollbar-thin">
            {queryText}
          </pre>

          {/* Result summary */}
          {step.result_summary && (
            <div>
              <span className="text-muted-foreground">Result: </span>
              <span className="text-[11px]">{step.result_summary}</span>
            </div>
          )}
        </div>
      </div>

      {/* Footer action bar */}
      <div className="border-t px-3 py-1.5">
        {saveMode === "idle" && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setSaveMode("editing");
            }}
            className="flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground hover:text-primary transition-colors"
          >
            <Bookmark className="h-3 w-3" />
            Save to Analytics
          </button>
        )}

        {saveMode === "editing" && (
          <div className="flex items-center gap-2">
            <div className="relative flex-1 min-w-0">
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Query name..."
                className="w-full rounded-md border bg-background px-2.5 py-1 pr-7 text-[11px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                autoFocus
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleSave();
                  if (e.key === "Escape") setSaveMode("idle");
                }}
              />
              <Pencil className="absolute right-2 top-1/2 -translate-y-1/2 h-3 w-3 text-muted-foreground/50" />
            </div>
            <button
              onClick={handleSave}
              disabled={!name.trim() || mutation.isPending}
              className="shrink-0 flex items-center gap-1 rounded-md bg-primary px-2.5 py-1 text-[11px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors"
            >
              {mutation.isPending ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                "Save"
              )}
            </button>
            <button
              onClick={() => setSaveMode("idle")}
              className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground transition-colors"
            >
              <X className="h-3 w-3" />
            </button>
            {mutation.isError && (
              <span className="text-[11px] text-destructive truncate">
                Failed to save
              </span>
            )}
          </div>
        )}

        {saveMode === "saved" && (
          <div className="flex items-center gap-1.5 text-[11px] font-medium text-green-600 dark:text-green-400">
            <Check className="h-3 w-3" />
            Saved to Analytics
          </div>
        )}
      </div>
    </div>
  );
}
