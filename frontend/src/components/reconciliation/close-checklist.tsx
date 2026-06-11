"use client";

import { useState } from "react";
import { CheckCircle2, Circle, Lock, AlertTriangle } from "lucide-react";
import { cn } from "@/lib/utils";
import { useClosePeriod } from "@/hooks/use-reconciliation";
import type { ReconBucketSummary, ReconRun } from "@/lib/types";

interface CloseChecklistProps {
  run: ReconRun | null;
  /** Server-computed bucket summary (useReconBucketSummary). The auto-checks
   *  key on summary.close_readiness — live SQL counts over the FULL run (the
   *  old results-prop scan only ever saw one page at production scale).
   *  undefined while loading — every auto-check FAILS CLOSED until it arrives
   *  (manual toggles still work: HITL checklist semantics unchanged). */
  summary: ReconBucketSummary | undefined;
  period: string;
}

interface ChecklistStep {
  id: string;
  label: string;
  description: string;
  check: (run: ReconRun | null, summary: ReconBucketSummary | undefined) => boolean;
}

// Auto-checks compare against `=== 0` so a missing summary/close_readiness
// (loading, or deploy-skew payload from an older backend) yields `undefined
// === 0` → false → incomplete. Fail closed — never report "ready" on absent data.
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
    check: (_, summary) => summary?.close_readiness?.open_exceptions === 0,
  },
  {
    id: "approve_matches",
    label: "Approve Suggested Matches",
    description: "Review and approve all suggested matches",
    check: (_, summary) => summary?.close_readiness?.suggested === 0,
  },
  {
    id: "review_material_variances",
    label: "Review Material Variances",
    description:
      "Approve or investigate confident matches with material variance — close will NOT lock these",
    // left_for_review mirrors the backend close_period() left-for-review
    // predicate (api/v1/reconciliation.py lock_predicate/skipped_stmt): rows
    // with status="auto_matched" AND bucket="needs_review" are deliberately
    // left unlocked on close (HITL — a material variance is never silently
    // buried), so the checklist must not report "ready" while any remain.
    // Counted server-side over the FULL run, keyed on the authoritative
    // status + bucket only — never the advisory confidence.
    check: (_, summary) => summary?.close_readiness?.left_for_review === 0,
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

export function CloseChecklist({ run, summary, period }: CloseChecklistProps) {
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
    return step.check(run, summary);
  };

  // Every step except the lock itself gates the lock (was a positional
  // slice(0, 4); keyed by id so inserting steps cannot silently un-gate).
  const allPrereqsMet = STEPS.filter((s) => s.id !== "lock_period").every((s) => isComplete(s));

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
              data-testid={step.id}
              data-complete={complete}
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
