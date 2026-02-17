"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight, Wrench } from "lucide-react";
import type { ToolCallStep } from "@/lib/types";

interface ToolCallStepCardProps {
  step: ToolCallStep;
}

export function ToolCallStepCard({ step }: ToolCallStepCardProps) {
  const [open, setOpen] = useState(false);

  return (
    <div className="rounded border bg-background/50 text-xs">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-2 py-1.5 hover:bg-accent/50 transition-colors"
      >
        {open ? (
          <ChevronDown className="h-3 w-3 shrink-0" />
        ) : (
          <ChevronRight className="h-3 w-3 shrink-0" />
        )}
        <Wrench className="h-3 w-3 shrink-0 text-muted-foreground" />
        <span className="font-medium truncate">{step.tool}</span>
        <span className="ml-auto shrink-0 rounded bg-muted px-1.5 py-0.5 text-muted-foreground">
          {step.duration_ms}ms
        </span>
      </button>
      {open && (
        <div className="border-t px-2 py-1.5 space-y-1">
          <div>
            <span className="text-muted-foreground">Params: </span>
            <code className="break-all">{JSON.stringify(step.params)}</code>
          </div>
          <div>
            <span className="text-muted-foreground">Result: </span>
            <span>{step.result_summary}</span>
          </div>
        </div>
      )}
    </div>
  );
}
