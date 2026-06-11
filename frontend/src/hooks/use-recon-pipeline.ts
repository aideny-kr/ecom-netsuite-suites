"use client";

import { useState, useCallback, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

export type PipelineStageId =
  | "preflight"
  | "sync_stripe"
  | "sync_netsuite"
  | "matching"
  | "classifying"
  | "complete";

export type StageStatus = "pending" | "running" | "completed" | "skipped" | "error";

export interface PipelineStage {
  id: PipelineStageId;
  label: string;
  status: StageStatus;
  message: string;
}

export interface ReconPipelineState {
  isRunning: boolean;
  stages: PipelineStage[];
  progress: number;
  error: string | null;
  runId: string | null;
  summary: ReconPipelineSummary | null;
}

export interface ReconPipelineSummary {
  run_id: string;
  total_payouts: number;
  total_deposits: number;
  matched_count: number;
  exception_count: number;
  unmatched_count: number;
  total_variance: string;
  match_rate: string;
}

const DEFAULT_STAGES: PipelineStage[] = [
  { id: "preflight", label: "Validating connections", status: "pending", message: "" },
  { id: "sync_stripe", label: "Syncing Stripe payouts", status: "pending", message: "" },
  { id: "sync_netsuite", label: "Syncing NetSuite deposits", status: "pending", message: "" },
  { id: "matching", label: "Running matching engine", status: "pending", message: "" },
  { id: "classifying", label: "Classifying results", status: "pending", message: "" },
  { id: "complete", label: "Finalizing", status: "pending", message: "" },
];

export function useReconPipeline() {
  const queryClient = useQueryClient();
  const [state, setState] = useState<ReconPipelineState>({
    isRunning: false,
    stages: DEFAULT_STAGES,
    progress: 0,
    error: null,
    runId: null,
    summary: null,
  });
  const abortRef = useRef<AbortController | null>(null);

  const runPipeline = useCallback(
    async (params: { date_from: string; date_to: string; subsidiary_id?: string }) => {
      // Reset state
      setState({
        isRunning: true,
        stages: DEFAULT_STAGES.map((s) => ({ ...s, status: "pending", message: "" })),
        progress: 0,
        error: null,
        runId: null,
        summary: null,
      });

      try {
        const response = await apiClient.stream(
          "/api/v1/reconciliation/runs/stream",
          params,
        );

        const reader = response.body?.getReader();
        if (!reader) throw new Error("Stream not available");

        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const chunks = buffer.split("\n\n");
          buffer = chunks.pop() || "";

          for (const chunk of chunks) {
            const dataLines = chunk
              .split("\n")
              .filter((line) => line.startsWith("data: "))
              .map((line) => line.slice(6).trim())
              .filter(Boolean);

            if (dataLines.length === 0) continue;

            try {
              const event = JSON.parse(dataLines.join("\n"));
              handleEvent(event);
            } catch {
              // Skip malformed events
            }
          }
        }
      } catch (err) {
        setState((prev) => ({
          ...prev,
          isRunning: false,
          error: err instanceof Error ? err.message : "Pipeline failed",
        }));
      }
    },
    [],
  );

  const handleEvent = useCallback(
    (event: Record<string, unknown>) => {
      const type = event.type as string;

      if (type === "recon_progress") {
        const stageId = event.stage as PipelineStageId;
        const status = event.status as StageStatus;
        const message = (event.message as string) || "";
        const progress = (event.progress as number) || 0;

        setState((prev) => ({
          ...prev,
          progress,
          stages: prev.stages.map((s) =>
            s.id === stageId ? { ...s, status, message } : s,
          ),
        }));
      } else if (type === "recon_complete") {
        const summary: ReconPipelineSummary = {
          run_id: event.run_id as string,
          total_payouts: event.total_payouts as number,
          total_deposits: event.total_deposits as number,
          matched_count: event.matched_count as number,
          exception_count: event.exception_count as number,
          unmatched_count: event.unmatched_count as number,
          total_variance: event.total_variance as string,
          match_rate: event.match_rate as string,
        };
        setState((prev) => ({
          ...prev,
          isRunning: false,
          runId: summary.run_id,
          summary,
          progress: 100,
        }));
        // A completed run wrote new results AND changed the period close
        // scope + readiness counts the CloseChecklist gates on (R4-A #3:
        // only invalidating recon-runs left a green-STALE checklist that
        // could gate a close freezing the new run's unreviewed rows).
        queryClient.invalidateQueries({ queryKey: ["recon-runs"] });
        queryClient.invalidateQueries({ queryKey: ["recon-results"] });
        queryClient.invalidateQueries({ queryKey: ["recon-bucket-summary"] });
        queryClient.invalidateQueries({ queryKey: ["recon-close-readiness"] });
      } else if (type === "recon_error") {
        setState((prev) => ({
          ...prev,
          isRunning: false,
          error: (event.error as string) || "Unknown error",
        }));
      }
    },
    [queryClient],
  );

  const reset = useCallback(() => {
    setState({
      isRunning: false,
      stages: DEFAULT_STAGES,
      progress: 0,
      error: null,
      runId: null,
      summary: null,
    });
  }, []);

  return {
    ...state,
    runPipeline,
    reset,
  };
}
