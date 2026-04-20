"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MasterDetailRunView } from "./master-detail-run-view";
import { useAgentLabRun } from "@/hooks/use-agent-lab-run";
import {
  cancelRun,
  getRunSnapshot,
  listRuns,
  startRun,
  type CaseResult,
} from "@/lib/agent-lab";

// Placeholder case IDs for single-run mode. v1.1 will fetch from the API
// (possibly via a new GET /agent-lab/eval-cases?kind=benchmark endpoint).
const BENCHMARK_CASE_IDS = [
  "country.norway_sales",
  "country.all_orders",
  "class.refurb_split",
  "platform.shopify_mix",
];

export function BenchmarkTab() {
  const qc = useQueryClient();
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
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
      qc.invalidateQueries({ queryKey: ["agent-lab-runs", "benchmark"] });
    },
  });

  const cancelMut = useMutation({
    mutationFn: (runId: string) => cancelRun(runId),
  });

  const isRunning =
    !!activeRunId &&
    (live.status === "running" || live.status === "preparing" || live.status === "connecting");

  return (
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
  );
}
