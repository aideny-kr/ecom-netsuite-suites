"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { ReconRun, ReconResult, ReconRunSummary } from "@/lib/types";

export function useReconRuns() {
  return useQuery<ReconRun[]>({
    queryKey: ["recon-runs"],
    queryFn: () => apiClient.get<ReconRun[]>("/api/v1/reconciliation/runs"),
  });
}

export function useReconResults(runId: string | null, statusFilter?: string) {
  const params = new URLSearchParams();
  if (statusFilter) params.set("status_filter", statusFilter);

  return useQuery<ReconResult[]>({
    queryKey: ["recon-results", runId, statusFilter],
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
