"use client";

import { useState } from "react";
import { ChevronDown, Wrench, FolderTree, FileSearch, FileCode, GitPullRequest } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ToolCallStep } from "@/lib/types";

interface ToolCallStepCardProps {
  step: ToolCallStep;
}

function getToolIcon(toolName: string) {
  if (toolName.startsWith("workspace_list")) return <FolderTree className="h-3 w-3 shrink-0 text-muted-foreground" />;
  if (toolName.startsWith("workspace_search")) return <FileSearch className="h-3 w-3 shrink-0 text-muted-foreground" />;
  if (toolName.startsWith("workspace_read")) return <FileCode className="h-3 w-3 shrink-0 text-muted-foreground" />;
  if (toolName.startsWith("workspace_propose")) return <GitPullRequest className="h-3 w-3 shrink-0 text-muted-foreground" />;
  return <Wrench className="h-3 w-3 shrink-0 text-muted-foreground" />;
}

export function ToolCallStepCard({ step }: ToolCallStepCardProps) {
  const [open, setOpen] = useState(false);

  return (
    <div className="rounded-lg border bg-background/80 text-[12px]">
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
        {getToolIcon(step.tool)}
        <span className="font-medium truncate">{step.tool}</span>
        <span className="ml-auto shrink-0 rounded-md bg-muted px-1.5 py-0.5 text-[11px] tabular-nums text-muted-foreground">
          {step.duration_ms}ms
        </span>
      </button>
      <div
        className={cn(
          "overflow-hidden transition-all duration-200",
          open ? "max-h-[500px]" : "max-h-0",
        )}
      >
        <div className="border-t px-3 py-2 space-y-1.5">
          <div>
            <span className="text-muted-foreground">Params: </span>
            <code className="break-all text-[11px]">{JSON.stringify(step.params)}</code>
          </div>
          <div>
            <span className="text-muted-foreground">Result: </span>
            <span>{step.result_summary}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
