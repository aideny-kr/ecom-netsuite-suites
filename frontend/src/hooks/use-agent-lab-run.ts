"use client";

import { useEffect, useState, useRef } from "react";
import type { CaseResult } from "@/lib/agent-lab";
import { apiClient } from "@/lib/api-client";
import { consumeAgentLabStream } from "@/lib/agent-lab-stream";

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
  // Track the last received event ID per run so that on remount (e.g. navigate
  // away and back) the backend resumes from where it left off rather than
  // replaying all prior events from 0-0. Heartbeats (which carry no id) do not
  // advance this cursor — the parser guards that.
  const lastEventIdRef = useRef<string>("0-0");

  useEffect(() => {
    if (!runId) return;

    // Fetch-based SSE via apiClient.streamGet. The browser's native SSE API
    // cannot set Authorization headers, and this project's access_token lives
    // in localStorage (not a cookie), so fetch streaming is the only way to
    // authenticate against the backend's Bearer-token auth.
    const controller = new AbortController();

    // Pass last_id so backend sends only events after what we've already seen.
    // On first mount, lastEventIdRef.current is "0-0" which means "send all".
    const resumeId = lastEventIdRef.current;
    const url = `/api/v1/agent-lab/runs/${runId}/events?last_id=${encodeURIComponent(resumeId)}`;

    (async () => {
      try {
        const response = await apiClient.streamGet(url, controller.signal);
        await consumeAgentLabStream(
          response,
          {
            onPreparing: (data) => {
              setStatus("preparing");
              setPreparingPhase(data.phase);
            },
            onRunStarted: (data) => {
              setStatus("running");
              setTotalCases(data.total_cases);
            },
            onCaseComplete: (data) => {
              setCasesCompleted(data.cases_completed);
              setRunningCost(data.running_cost_usd);
              // Dedupe by case_id — guards against reconnect-replay race
              // conditions where the backend re-emits an event we already
              // processed.
              setCases((prev) => {
                if (
                  prev.some(
                    (c) => "case_id" in c && c.case_id === data.case_id,
                  )
                ) {
                  return prev;
                }
                return [...prev, data.result];
              });
            },
            onRunComplete: (data) => {
              setStatus(data.status);
              if (data.error_message) setError(data.error_message);
            },
            onError: (message) => {
              setError(message);
              setStatus("failed");
              // Parser stops itself on error; we do not retry. A transparent
              // retry path would risk replaying from 0-0 without the
              // ?last_id= param and produce duplicate cases.
            },
            onEventId: (id) => {
              lastEventIdRef.current = id;
            },
          },
          controller.signal,
        );
      } catch (err) {
        // Abort on unmount is expected; don't surface it as an error.
        if (controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof Error ? err.message : String(err));
        setStatus("failed");
      }
    })();

    return () => {
      controller.abort();
    };
  }, [runId]);

  return {
    status,
    preparingPhase,
    totalCases,
    casesCompleted,
    runningCost,
    cases,
    error,
  };
}
