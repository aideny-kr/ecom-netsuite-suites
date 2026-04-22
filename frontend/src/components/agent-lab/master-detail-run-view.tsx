"use client";

import { useState } from "react";
import type { AgentLabRun, CaseResult, RunKind } from "@/lib/agent-lab";
import type { RunHookStatus } from "@/hooks/use-agent-lab-run";
import { CaseDetailPane } from "./case-detail-pane";
import { RunControls } from "./run-controls";
import { RunProgressBar } from "./run-progress-bar";

interface Props {
  kind: RunKind;
  runs: AgentLabRun[];
  activeRun: AgentLabRun | null;
  cases: CaseResult[];
  caseIds: string[];
  estimatedCost: number;
  isRunning: boolean;
  runStatus: RunHookStatus;
  casesCompleted: number;
  totalCases: number;
  runningCost: number;
  preparingPhase?: string | null;
  allowSingleCase?: boolean;
  onRunAll: () => void;
  onRunSingle: (caseId: string) => void;
  onCancel: () => void;
  onSelectRun: (runId: string) => void;
}

export function MasterDetailRunView(props: Props) {
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const selectedCase =
    props.cases.find((c) => {
      const cid = "case_id" in c ? c.case_id : null;
      return cid === selectedCaseId;
    }) ?? null;

  return (
    <div className="flex h-full gap-4">
      {/* Left rail */}
      <div className="w-64 space-y-3 overflow-y-auto">
        <RunProgressBar
          casesCompleted={props.casesCompleted}
          totalCases={props.totalCases}
          runningCost={props.runningCost}
          status={props.runStatus}
          preparingPhase={props.preparingPhase}
        />
        <RunControls
          kind={props.kind}
          caseIds={props.caseIds}
          estimatedCost={props.estimatedCost}
          canRun={!props.isRunning}
          onRunAll={props.onRunAll}
          onRunSingle={props.onRunSingle}
          onCancel={props.onCancel}
          isRunning={props.isRunning}
          allowSingleCase={props.allowSingleCase}
        />
        <div className="rounded-xl border bg-card p-3 space-y-1">
          <div className="text-[11px] uppercase text-muted-foreground">Recent runs</div>
          {props.runs.map((r) => (
            <button
              key={r.run_id}
              onClick={() => props.onSelectRun(r.run_id)}
              className="block w-full text-left rounded px-2 py-1 text-[12px] hover:bg-accent"
            >
              {new Date(r.started_at ?? "").toLocaleString()} · {r.status}
            </button>
          ))}
        </div>
        <div className="rounded-xl border bg-card p-3 space-y-1">
          <div className="text-[11px] uppercase text-muted-foreground">Cases</div>
          {props.cases.map((c, i) => {
            const cid = "case_id" in c ? c.case_id : `case-${i}`;
            return (
              <button
                key={cid}
                onClick={() => setSelectedCaseId(cid)}
                className="block w-full text-left rounded px-2 py-1 text-[12px] hover:bg-accent"
              >
                {cid}
              </button>
            );
          })}
        </div>
      </div>

      {/* Right pane */}
      <div className="flex-1 min-w-0 rounded-xl border bg-card overflow-auto">
        <CaseDetailPane caseResult={selectedCase} kind={props.kind} />
      </div>
    </div>
  );
}
