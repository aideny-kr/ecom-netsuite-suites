"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type {
  ReconRun,
  ReconResult,
  ReconRunSummary,
  ReconBucketSummary,
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
      // The CloseChecklist keys on ["recon-bucket-summary", runId] — a
      // single-row approve must refresh its counts too (prefix match
      // invalidates every run's summary; mirrors useApproveBucket).
      queryClient.invalidateQueries({ queryKey: ["recon-bucket-summary"] });
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
    },
  });
}
