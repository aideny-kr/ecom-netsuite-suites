"use client";

import { CheckCircle2, Circle, Loader2, SkipForward, AlertCircle } from "lucide-react";
import type { PipelineStage, StageStatus, ReconPipelineSummary } from "@/hooks/use-recon-pipeline";

interface ReconProgressStepperProps {
  stages: PipelineStage[];
  progress: number;
  error: string | null;
  summary: ReconPipelineSummary | null;
}

function StageIcon({ status }: { status: StageStatus }) {
  switch (status) {
    case "completed":
      return <CheckCircle2 className="h-5 w-5 text-emerald-500 shrink-0" />;
    case "running":
      return <Loader2 className="h-5 w-5 text-blue-500 animate-spin shrink-0" />;
    case "skipped":
      return <SkipForward className="h-5 w-5 text-muted-foreground/50 shrink-0" />;
    case "error":
      return <AlertCircle className="h-5 w-5 text-red-500 shrink-0" />;
    default:
      return <Circle className="h-5 w-5 text-muted-foreground/30 shrink-0" />;
  }
}

function StageConnector({ active }: { active: boolean }) {
  return (
    <div
      className={`hidden sm:block h-0.5 flex-1 min-w-[24px] transition-colors duration-500 ${
        active ? "bg-emerald-500" : "bg-muted-foreground/15"
      }`}
    />
  );
}

export function ReconProgressStepper({
  stages,
  progress,
  error,
  summary,
}: ReconProgressStepperProps) {
  return (
    <div className="rounded-xl border bg-card p-5 shadow-soft space-y-4">
      {/* Progress bar */}
      <div className="flex items-center justify-between text-[13px] text-muted-foreground">
        <span className="font-medium text-foreground">Reconciliation Pipeline</span>
        <span>{Math.round(progress)}%</span>
      </div>
      <div className="h-1.5 w-full rounded-full bg-muted-foreground/10 overflow-hidden">
        <div
          className="h-full rounded-full bg-blue-500 transition-all duration-700 ease-out"
          style={{ width: `${progress}%` }}
        />
      </div>

      {/* Horizontal stage stepper */}
      <div className="flex items-start gap-0 sm:gap-0 overflow-x-auto pb-2">
        {stages.map((stage, i) => (
          <div key={stage.id} className="flex items-center flex-1 min-w-0">
            <div className="flex flex-col items-center text-center min-w-[80px] max-w-[140px]">
              <StageIcon status={stage.status} />
              <span
                className={`mt-1.5 text-[11px] leading-tight font-medium ${
                  stage.status === "running"
                    ? "text-blue-600"
                    : stage.status === "completed"
                      ? "text-emerald-600"
                      : stage.status === "skipped"
                        ? "text-muted-foreground/50"
                        : stage.status === "error"
                          ? "text-red-600"
                          : "text-muted-foreground"
                }`}
              >
                {stage.label}
              </span>
              {stage.message && (
                <span className="mt-0.5 text-[10px] text-muted-foreground max-w-[130px] truncate">
                  {stage.message}
                </span>
              )}
            </div>
            {i < stages.length - 1 && (
              <StageConnector
                active={
                  stage.status === "completed" || stage.status === "skipped"
                }
              />
            )}
          </div>
        ))}
      </div>

      {/* Error message */}
      {error && (
        <div className="flex items-start gap-2 rounded-lg bg-red-500/10 p-3">
          <AlertCircle className="h-4 w-4 text-red-500 mt-0.5 shrink-0" />
          <p className="text-[13px] text-red-600">{error}</p>
        </div>
      )}

      {/* Summary after completion */}
      {summary && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-2 border-t border-border/50">
          <SummaryStat label="Payouts" value={summary.total_payouts} />
          <SummaryStat label="Deposits" value={summary.total_deposits} />
          <SummaryStat label="Matched" value={summary.matched_count} accent="emerald" />
          <SummaryStat
            label="Match Rate"
            value={`${Number(summary.match_rate).toFixed(1)}%`}
            accent={Number(summary.match_rate) >= 90 ? "emerald" : "amber"}
          />
        </div>
      )}
    </div>
  );
}

function SummaryStat({
  label,
  value,
  accent,
}: {
  label: string;
  value: number | string;
  accent?: "emerald" | "amber";
}) {
  const colorClass = accent === "emerald"
    ? "text-emerald-600"
    : accent === "amber"
      ? "text-amber-600"
      : "text-foreground";

  return (
    <div className="text-center">
      <p className={`text-lg font-semibold ${colorClass}`}>{value}</p>
      <p className="text-[11px] text-muted-foreground">{label}</p>
    </div>
  );
}
