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
  // Track the last received event ID per run so that on remount (e.g. navigate
  // away and back) the backend resumes from where it left off rather than
  // replaying all prior events from 0-0.
  const lastEventIdRef = useRef<string>("0-0");

  useEffect(() => {
    if (!runId) return;

    // Pass last_id so backend sends only events after what we've already seen.
    // On first mount, lastEventIdRef.current is "0-0" which means "send all".
    const resumeId = lastEventIdRef.current;
    const url = `/api/v1/agent-lab/runs/${runId}/events?last_id=${encodeURIComponent(resumeId)}`;
    const es = new EventSource(url, { withCredentials: true });
    esRef.current = es;

    // Capture the event ID from each SSE message so we can resume correctly.
    const trackId = (evt: MessageEvent) => {
      if (evt.lastEventId) {
        lastEventIdRef.current = evt.lastEventId;
      }
    };

    es.addEventListener("preparing", (evt) => {
      trackId(evt as MessageEvent);
      const data = JSON.parse((evt as MessageEvent).data) as Extract<
        AgentLabEvent,
        { type: "preparing" }
      >;
      setStatus("preparing");
      setPreparingPhase(data.phase);
    });

    es.addEventListener("run_started", (evt) => {
      trackId(evt as MessageEvent);
      const data = JSON.parse((evt as MessageEvent).data);
      setStatus("running");
      setTotalCases(data.total_cases);
    });

    es.addEventListener("case_complete", (evt) => {
      trackId(evt as MessageEvent);
      const data = JSON.parse((evt as MessageEvent).data);
      setCasesCompleted(data.cases_completed);
      setRunningCost(data.running_cost_usd);
      // Dedupe by case_id — guards against reconnect-replay race conditions
      // where the backend re-emits an event we already processed.
      setCases((prev) => {
        if (prev.some((c) => "case_id" in c && c.case_id === data.case_id)) {
          return prev;
        }
        return [...prev, data.result as CaseResult];
      });
    });

    es.addEventListener("run_complete", (evt) => {
      trackId(evt as MessageEvent);
      const data = JSON.parse((evt as MessageEvent).data);
      setStatus(data.status);
      if (data.error_message) setError(data.error_message);
      es.close();
    });

    es.addEventListener("error", () => {
      setError("connection error");
      // Close on error — browser native retry would replay from 0-0 without
      // the ?last_id= query param, causing duplicate case entries.
      es.close();
    });

    return () => {
      es.close();
    };
  }, [runId]);

  return { status, preparingPhase, totalCases, casesCompleted, runningCost, cases, error };
}
