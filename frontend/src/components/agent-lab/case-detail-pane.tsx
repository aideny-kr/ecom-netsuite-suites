"use client";

import type {
  CaseResult,
  BenchmarkCaseResult,
  ExperimentCaseResult,
} from "@/lib/agent-lab";

interface Props {
  caseResult: CaseResult | null;
  kind: "benchmark" | "experiment";
}

export function CaseDetailPane({ caseResult, kind }: Props) {
  if (!caseResult) {
    return (
      <div className="flex items-center justify-center h-full text-[13px] text-muted-foreground">
        Select a case to view detail
      </div>
    );
  }

  if (kind === "benchmark" && "ours" in caseResult) {
    const c = caseResult as BenchmarkCaseResult;
    return (
      <div className="p-5 space-y-4">
        <h3 className="text-[15px] font-medium">{c.case_id}</h3>
        <div className="grid grid-cols-2 gap-4">
          <div className="rounded-lg border bg-green-50 p-4 space-y-2">
            <div className="text-[11px] uppercase text-muted-foreground">
              Ours · {c.ours?.accuracy?.toFixed(2) ?? "—"}
            </div>
            <p className="text-[13px] whitespace-pre-wrap">
              {c.ours?.answer_preview || c.ours?.error_message || "—"}
            </p>
            <div className="text-[11px] text-muted-foreground">
              ${c.ours?.cost_usd?.toFixed(3) ?? "—"} · {c.ours?.latency_ms ?? "—"}ms
            </div>
          </div>
          <div className="rounded-lg border bg-orange-50 p-4 space-y-2">
            <div className="text-[11px] uppercase text-muted-foreground">
              MCP · {c.mcp?.accuracy?.toFixed(2) ?? "—"}
            </div>
            <p className="text-[13px] whitespace-pre-wrap">
              {c.mcp?.answer_preview || c.mcp?.error_message || "—"}
            </p>
            <div className="text-[11px] text-muted-foreground">
              ${c.mcp?.cost_usd?.toFixed(3) ?? "—"} · {c.mcp?.latency_ms ?? "—"}ms
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Experiment
  const c = caseResult as ExperimentCaseResult;
  return (
    <div className="p-5 space-y-4">
      <h3 className="text-[15px] font-medium">{c.case_id}</h3>
      <div className="flex gap-2 text-[11px]">
        <span className="rounded-full bg-muted px-2 py-0.5">{c.dialect}</span>
        <span className="rounded-full bg-muted px-2 py-0.5">
          decision: {c.decision}
        </span>
        <span className="rounded-full bg-muted px-2 py-0.5">
          score: {c.experiment_score.toFixed(2)}
        </span>
      </div>
      <div>
        <div className="text-[11px] uppercase text-muted-foreground mb-1">
          Generated SQL
        </div>
        <pre className="rounded-lg border bg-muted p-3 text-[12px] overflow-x-auto">
          {c.generated_sql || "(no SQL generated)"}
        </pre>
      </div>
      {c.error_message && (
        <div className="rounded-lg border border-destructive bg-destructive/10 p-3 text-[13px]">
          {c.error_message}
        </div>
      )}
    </div>
  );
}
