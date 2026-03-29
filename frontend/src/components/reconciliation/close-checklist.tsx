"use client";

import { useState } from "react";
import { CheckCircle2, Circle, Lock, AlertTriangle } from "lucide-react";
import { cn } from "@/lib/utils";
import { useClosePeriod } from "@/hooks/use-reconciliation";
import type { ReconRun, ReconResult } from "@/lib/types";

interface CloseChecklistProps {
  run: ReconRun | null;
  results: ReconResult[];
  period: string;
}

interface ChecklistStep {
  id: string;
  label: string;
  description: string;
  check: (run: ReconRun | null, results: ReconResult[]) => boolean;
}

const STEPS: ChecklistStep[] = [
  {
    id: "run_recon",
    label: "Run Reconciliation",
    description: "Execute matching engine for the period",
    check: (run) => run?.status === "completed" || run?.status === "closed",
  },
  {
    id: "review_exceptions",
    label: "Review Exceptions",
    description: "Investigate and resolve all exceptions",
    check: (_, results) => {
      const pending = results.filter((r) => r.status === "pending" && r.match_type !== "unmatched");
      return pending.length === 0;
    },
  },
  {
    id: "approve_matches",
    label: "Approve Suggested Matches",
    description: "Review and approve all suggested matches",
    check: (_, results) => {
      const suggested = results.filter((r) => r.status === "suggested");
      return suggested.length === 0;
    },
  },
  {
    id: "export_evidence",
    label: "Export Evidence Pack",
    description: "Download evidence for audit trail",
    check: () => false, // Manual step — user must confirm
  },
  {
    id: "lock_period",
    label: "Lock Period",
    description: "Lock all approved matches (irreversible)",
    check: (run) => run?.status === "closed",
  },
];

export function CloseChecklist({ run, results, period }: CloseChecklistProps) {
  const closePeriod = useClosePeriod();
  const [manualChecks, setManualChecks] = useState<Set<string>>(new Set());

  const toggleManual = (stepId: string) => {
    setManualChecks((prev) => {
      const next = new Set(prev);
      if (next.has(stepId)) next.delete(stepId);
      else next.add(stepId);
      return next;
    });
  };

  const isComplete = (step: ChecklistStep) => {
    if (manualChecks.has(step.id)) return true;
    return step.check(run, results);
  };

  const allPrereqsMet = STEPS.slice(0, 4).every((s) => isComplete(s));

  const handleLockPeriod = () => {
    if (!period) return;
    if (!confirm(`Are you sure you want to lock period ${period}? This cannot be undone.`)) return;
    closePeriod.mutate(period);
  };

  return (
    <div className="rounded-xl border bg-card p-5 shadow-soft">
      <h2 className="text-[15px] font-semibold text-foreground mb-4">
        Month-End Close — {period || "Select Period"}
      </h2>

      <div className="space-y-3">
        {STEPS.map((step) => {
          const complete = isComplete(step);
          const isLockStep = step.id === "lock_period";

          return (
            <div
              key={step.id}
              className={cn(
                "flex items-center gap-3 rounded-lg p-3 transition-colors",
                complete ? "bg-green-50" : "bg-muted/30",
              )}
            >
              <button
                onClick={() => {
                  if (!isLockStep) toggleManual(step.id);
                }}
                disabled={isLockStep}
                className="shrink-0"
              >
                {complete ? (
                  <CheckCircle2 className="h-5 w-5 text-green-600" />
                ) : (
                  <Circle className="h-5 w-5 text-muted-foreground" />
                )}
              </button>
              <div className="flex-1">
                <p className={cn("text-[13px] font-medium", complete ? "text-green-800" : "text-foreground")}>
                  {step.label}
                </p>
                <p className="text-[11px] text-muted-foreground">{step.description}</p>
              </div>
              {isLockStep && !complete && (
                <button
                  onClick={handleLockPeriod}
                  disabled={!allPrereqsMet || closePeriod.isPending}
                  className="flex items-center gap-1.5 rounded-lg bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-700 disabled:opacity-50 transition-colors"
                >
                  <Lock className="h-3.5 w-3.5" />
                  {closePeriod.isPending ? "Locking..." : "Lock Period"}
                </button>
              )}
            </div>
          );
        })}
      </div>

      {!allPrereqsMet && (
        <div className="mt-4 flex items-center gap-2 rounded-lg bg-orange-50 p-3 text-[13px] text-orange-800">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          Complete all steps before locking the period.
        </div>
      )}
    </div>
  );
}
