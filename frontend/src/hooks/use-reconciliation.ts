"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type {
  ReconRun,
  ReconResult,
  ReconRunSummary,
  ReconBucketSummary,
  ReconCloseReadiness,
} from "@/lib/types";

export function useReconRuns() {
  return useQuery<ReconRun[]>({
    queryKey: ["recon-runs"],
    queryFn: () => apiClient.get<ReconRun[]>("/api/v1/reconciliation/runs"),
  });
}

export function useReconResults(
  runId: string | null,
  statusFilter?: string,
  bucket?: string
) {
  const params = new URLSearchParams();
  if (statusFilter) params.set("status_filter", statusFilter);
  if (bucket) params.set("bucket", bucket);

  return useQuery<ReconResult[]>({
    queryKey: ["recon-results", runId, statusFilter, bucket],
    queryFn: () =>
      apiClient.get<ReconResult[]>(
        `/api/v1/reconciliation/runs/${runId}/results?${params.toString()}`
      ),
    enabled: !!runId,
  });
}

export function useCreateReconRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { date_from: string; date_to: string; subsidiary_id?: string }) =>
      apiClient.post<ReconRunSummary>("/api/v1/reconciliation/runs", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["recon-runs"] });
    },
  });
}

export function useApproveResult() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { result_id: string; notes?: string }) =>
      apiClient.patch<ReconResult>(
        `/api/v1/reconciliation/results/${data.result_id}/approve`,
        data
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["recon-results"] });
      // A single-row approve changes the bucket counts AND the period
      // close-readiness counts the CloseChecklist gates on (prefix match
      // invalidates every run's summary / every period's readiness).
      queryClient.invalidateQueries({ queryKey: ["recon-bucket-summary"] });
      queryClient.invalidateQueries({ queryKey: ["recon-close-readiness"] });
    },
  });
}

export function useClosePeriod() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (period: string) =>
      apiClient.post(`/api/v1/reconciliation/close/${period}`, {}),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["recon-runs"] });
      // Close locks rows server-side (status -> locked): the results table,
      // the bucket counts and the period readiness must refetch too.
      queryClient.invalidateQueries({ queryKey: ["recon-results"] });
      queryClient.invalidateQueries({ queryKey: ["recon-bucket-summary"] });
      queryClient.invalidateQueries({ queryKey: ["recon-close-readiness"] });
    },
  });
}

export function useReconBucketSummary(runId: string | null) {
  return useQuery<ReconBucketSummary>({
    queryKey: ["recon-bucket-summary", runId],
    queryFn: () =>
      apiClient.get<ReconBucketSummary>(
        `/api/v1/reconciliation/runs/${runId}/buckets`
      ),
    enabled: !!runId,
  });
}

/** PERIOD-scoped close readiness (R3-A): POST /close/{period} closes EVERY
 *  completed run in the month, so the CloseChecklist gate must aggregate over
 *  that same scope — never the selected run's bucket summary. */
export function useCloseReadiness(period: string | null) {
  return useQuery<ReconCloseReadiness>({
    queryKey: ["recon-close-readiness", period],
    queryFn: () =>
      apiClient.get<ReconCloseReadiness>(
        `/api/v1/reconciliation/close-readiness/${period}`
      ),
    enabled: !!period,
  });
}

export function useApproveBucket(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { bucket: string; notes?: string }) =>
      apiClient.post(
        `/api/v1/reconciliation/runs/${runId}/approve-bucket`,
        data
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["recon-results"] });
      queryClient.invalidateQueries({
        queryKey: ["recon-bucket-summary", runId],
      });
      queryClient.invalidateQueries({ queryKey: ["recon-runs"] });
      // Bulk approve drains suggested/left_for_review — the period readiness
      // the CloseChecklist gates on must refetch (prefix: every period).
      queryClient.invalidateQueries({ queryKey: ["recon-close-readiness"] });
    },
  });
}
