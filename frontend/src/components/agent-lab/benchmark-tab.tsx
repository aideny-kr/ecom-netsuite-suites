"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle } from "lucide-react";
import { MasterDetailRunView } from "./master-detail-run-view";
import { useAgentLabRun } from "@/hooks/use-agent-lab-run";
import {
  cancelRun,
  getRunSnapshot,
  listRuns,
  startRun,
  type CaseResult,
} from "@/lib/agent-lab";

// Representative case IDs for single-run mode. Must match `case_id` fields
// inside backend/app/services/benchmarks/benchmark_cases/vs_mcp/*.yaml —
// originally drifted from 4 placeholder strings that matched nothing, so
// every "Run single" dispatched 0 cases. v1.1 will fetch from the API
// (GET /agent-lab/eval-cases?kind=benchmark) to eliminate this hardcoded list.
const BENCHMARK_CASE_IDS = [
  "sales_country_canonical",
  "sales_country_single",
  "sales_top_customers",
  "sales_platform_top_5",
];

export function BenchmarkTab() {
  const qc = useQueryClient();
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const live = useAgentLabRun(activeRunId);

  const { data: runs = [] } = useQuery({
    queryKey: ["agent-lab-runs", "benchmark"],
    queryFn: () => listRuns({ kind: "benchmark", days: 14 }),
  });

  const activeRun = runs.find((r) => r.run_id === activeRunId) ?? null;
  const { data: snapshot } = useQuery({
    queryKey: ["agent-lab-run-snapshot", activeRunId],
    queryFn: () => (activeRunId ? getRunSnapshot(activeRunId) : null),
    enabled: !!activeRunId && live.status !== "running",
  });

  // Cases: prefer SSE-accumulated list during in-flight; otherwise snapshot
  const cases: CaseResult[] =
    live.status === "running" || live.status === "preparing"
      ? live.cases
      : snapshot?.cases ?? [];

  const estimatedCost =
    runs.slice(0, 5).reduce((acc, r) => acc + r.cost_usd_actual, 0) /
      Math.max(1, runs.slice(0, 5).length) || 6.3;

  const startMut = useMutation({
    mutationFn: (params: { mode: "all" | "single"; caseId?: string }) =>
      startRun({
        kind: "benchmark",
        mode: params.mode,
        case_id: params.caseId,
      }),
    onSuccess: (data) => {
      setActiveRunId(data.run_id);
      setRunError(null);
      qc.invalidateQueries({ queryKey: ["agent-lab-runs", "benchmark"] });
    },
    onError: (err: Error) => {
      const msg = err.message ?? "";
      if (msg.includes("409") || msg.toLowerCase().includes("already in progress") || msg.toLowerCase().includes("concurrent")) {
        setRunError("A run of this kind is already in progress. Cancel it first.");
      } else if (msg.includes("403") || msg.toLowerCase().includes("permission")) {
        setRunError("You don't have permission to run this.");
      } else {
        setRunError(`Failed to start run: ${msg || "unknown error"}`);
      }
    },
  });

  const cancelMut = useMutation({
    mutationFn: (runId: string) => cancelRun(runId),
    onError: (err: Error) => {
      setRunError(`Failed to cancel run: ${err.message ?? "unknown error"}`);
    },
  });

  const isRunning =
    !!activeRunId &&
    (live.status === "running" || live.status === "preparing" || live.status === "connecting");

  return (
    <div className="flex flex-col h-full gap-2">
      {runError && (
        <div className="flex items-center gap-2 rounded-md border border-destructive bg-destructive/10 px-3 py-2 text-[13px] text-destructive">
          <AlertCircle className="h-4 w-4 shrink-0" />
          {runError}
        </div>
      )}
      <div className="flex-1 min-h-0">
        <MasterDetailRunView
          kind="benchmark"
          runs={runs}
          activeRun={activeRun}
          cases={cases}
          caseIds={BENCHMARK_CASE_IDS}
          estimatedCost={estimatedCost}
          isRunning={isRunning}
          runStatus={live.status}
          casesCompleted={live.casesCompleted || activeRun?.cases_completed || 0}
          totalCases={live.totalCases || activeRun?.total_cases || 18}
          runningCost={live.runningCost}
          preparingPhase={live.preparingPhase}
          onRunAll={() => startMut.mutate({ mode: "all" })}
          onRunSingle={(caseId) => startMut.mutate({ mode: "single", caseId })}
          onCancel={() => activeRunId && cancelMut.mutate(activeRunId)}
          onSelectRun={(runId) => setActiveRunId(runId)}
        />
      </div>
    </div>
  );
}
