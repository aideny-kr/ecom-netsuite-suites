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

export function ExperimentsTab() {
  const qc = useQueryClient();
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const live = useAgentLabRun(activeRunId);

  const { data: runs = [] } = useQuery({
    queryKey: ["agent-lab-runs", "experiment"],
    queryFn: () => listRuns({ kind: "experiment", days: 14 }),
  });

  const activeRun = runs.find((r) => r.run_id === activeRunId) ?? null;
  const { data: snapshot } = useQuery({
    queryKey: ["agent-lab-run-snapshot", activeRunId],
    queryFn: () => (activeRunId ? getRunSnapshot(activeRunId) : null),
    enabled: !!activeRunId && live.status !== "running",
  });

  const cases: CaseResult[] =
    live.status === "running" || live.status === "preparing"
      ? live.cases
      : snapshot?.cases ?? [];

  const estimatedCost =
    runs.slice(0, 5).reduce((acc, r) => acc + r.cost_usd_actual, 0) /
      Math.max(1, runs.slice(0, 5).length) || 9.0;

  const startMut = useMutation({
    mutationFn: () => startRun({ kind: "experiment", mode: "all" }),
    onSuccess: (data) => {
      setActiveRunId(data.run_id);
      qc.invalidateQueries({ queryKey: ["agent-lab-runs", "experiment"] });
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
      kind="experiment"
      runs={runs}
      activeRun={activeRun}
      cases={cases}
      caseIds={[]}
      estimatedCost={estimatedCost}
      isRunning={isRunning}
      runStatus={live.status}
      casesCompleted={live.casesCompleted || activeRun?.cases_completed || 0}
      totalCases={live.totalCases || activeRun?.total_cases || 60}
      runningCost={live.runningCost}
      preparingPhase={live.preparingPhase}
      allowSingleCase={false}
      onRunAll={() => startMut.mutate()}
      onRunSingle={() => {}}
      onCancel={() => activeRunId && cancelMut.mutate(activeRunId)}
      onSelectRun={(runId) => setActiveRunId(runId)}
    />
  );
}
