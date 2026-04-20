"use client";

import { useEffect, useState, useRef } from "react";
import type { AgentLabEvent, CaseResult } from "@/lib/agent-lab";

export type RunHookStatus =
  | "connecting"
  | "preparing"
  | "running"
  | "completed"
  | "cancelled"
  | "failed";

export interface UseAgentLabRunResult {
  status: RunHookStatus;
  preparingPhase: string | null;
  totalCases: number;
  casesCompleted: number;
  runningCost: number;
  cases: CaseResult[];
  error: string | null;
}

export function useAgentLabRun(runId: string | null): UseAgentLabRunResult {
  const [status, setStatus] = useState<RunHookStatus>("connecting");
  const [preparingPhase, setPreparingPhase] = useState<string | null>(null);
  const [totalCases, setTotalCases] = useState(0);
  const [casesCompleted, setCasesCompleted] = useState(0);
  const [runningCost, setRunningCost] = useState(0);
  const [cases, setCases] = useState<CaseResult[]>([]);
  const [error, setError] = useState<string | null>(null);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!runId) return;
    const url = `/api/v1/agent-lab/runs/${runId}/events`;
    const es = new EventSource(url, { withCredentials: true });
    esRef.current = es;

    es.addEventListener("preparing", (evt) => {
      const data = JSON.parse((evt as MessageEvent).data) as Extract<
        AgentLabEvent,
        { type: "preparing" }
      >;
      setStatus("preparing");
      setPreparingPhase(data.phase);
    });

    es.addEventListener("run_started", (evt) => {
      const data = JSON.parse((evt as MessageEvent).data);
      setStatus("running");
      setTotalCases(data.total_cases);
    });

    es.addEventListener("case_complete", (evt) => {
      const data = JSON.parse((evt as MessageEvent).data);
      setCasesCompleted(data.cases_completed);
      setRunningCost(data.running_cost_usd);
      setCases((prev) => [...prev, data.result as CaseResult]);
    });

    es.addEventListener("run_complete", (evt) => {
      const data = JSON.parse((evt as MessageEvent).data);
      setStatus(data.status);
      if (data.error_message) setError(data.error_message);
      es.close();
    });

    es.addEventListener("error", () => {
      setError("connection error");
    });

    return () => {
      es.close();
    };
  }, [runId]);

  return { status, preparingPhase, totalCases, casesCompleted, runningCost, cases, error };
}
