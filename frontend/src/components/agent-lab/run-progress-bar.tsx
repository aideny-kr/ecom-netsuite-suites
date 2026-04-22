"use client";

import { cn } from "@/lib/utils";
import type { RunHookStatus } from "@/hooks/use-agent-lab-run";

interface Props {
  casesCompleted: number;
  totalCases: number;
  runningCost: number;
  status: RunHookStatus;
  preparingPhase?: string | null;
}

export function RunProgressBar({
  casesCompleted,
  totalCases,
  runningCost,
  status,
  preparingPhase,
}: Props) {
  if (status === "preparing") {
    return (
      <div className="rounded-xl border bg-card p-4">
        <div className="text-[13px] text-muted-foreground">
          Preparing — {preparingPhase || "loading"}…
        </div>
      </div>
    );
  }

  const pct = totalCases > 0 ? Math.round((casesCompleted / totalCases) * 100) : 0;

  return (
    <div className="rounded-xl border bg-card p-4 space-y-2">
      <div className="flex justify-between text-[13px]">
        <span>
          {casesCompleted} / {totalCases} complete
        </span>
        <span className="font-medium">${runningCost.toFixed(2)}</span>
      </div>
      <div className="h-2 w-full rounded-full bg-muted overflow-hidden">
        <div
          className={cn(
            "h-full transition-all",
            status === "failed" && "bg-destructive",
            status === "cancelled" && "bg-muted-foreground",
            (status === "running" || status === "completed") && "bg-primary",
          )}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
