"use client";

import { useState, useEffect } from "react";
import type { RunKind } from "@/lib/agent-lab";

interface Props {
  kind: RunKind;
  caseIds: string[];
  estimatedCost: number;
  canRun: boolean;
  onRunAll: () => void;
  onRunSingle: (caseId: string) => void;
  onCancel: () => void;
  isRunning: boolean;
  allowSingleCase?: boolean;
}

export function RunControls({
  kind,
  caseIds,
  estimatedCost,
  canRun,
  onRunAll,
  onRunSingle,
  onCancel,
  isRunning,
  allowSingleCase = true,
}: Props) {
  const [selectedCaseId, setSelectedCaseId] = useState(caseIds[0] ?? "");

  useEffect(() => {
    if (!selectedCaseId && caseIds[0]) {
      setSelectedCaseId(caseIds[0]);
    }
  }, [caseIds, selectedCaseId]);

  if (isRunning) {
    return (
      <div className="rounded-xl border bg-card p-4 space-y-2">
        <button
          onClick={onCancel}
          className="w-full rounded-md bg-destructive px-3 py-2 text-[13px] text-destructive-foreground hover:opacity-90"
        >
          Cancel
        </button>
        <p className="text-[11px] text-muted-foreground">
          Cancel takes effect at next case boundary (typically 5-30s).
        </p>
      </div>
    );
  }

  const totalCountLabel = kind === "benchmark" ? "18 cases" : "up to 60 cases";

  return (
    <div className="rounded-xl border bg-card p-4 space-y-3">
      <button
        onClick={onRunAll}
        disabled={!canRun}
        className="w-full rounded-md bg-primary px-3 py-2 text-[13px] text-primary-foreground hover:opacity-90 disabled:opacity-50"
      >
        ▶ Run all {totalCountLabel} · ~${estimatedCost.toFixed(2)}
      </button>
      {allowSingleCase && caseIds.length > 0 && (
        <div className="space-y-2 border-t pt-3">
          <label className="text-[11px] text-muted-foreground">Single case:</label>
          <select
            value={selectedCaseId}
            onChange={(e) => setSelectedCaseId(e.target.value)}
            className="w-full rounded-md border bg-background px-2 py-1 text-[13px]"
          >
            {caseIds.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
          <button
            onClick={() => selectedCaseId && onRunSingle(selectedCaseId)}
            disabled={!canRun || !selectedCaseId}
            className="w-full rounded-md border px-3 py-2 text-[13px] hover:bg-accent disabled:opacity-50"
          >
            ▶ Run single
          </button>
        </div>
      )}
    </div>
  );
}
