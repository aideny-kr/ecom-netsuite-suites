"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

interface ConnectorInfo {
  connected: boolean;
  status: string;
  last_sync: string | null;
  payout_count?: number;
  payout_line_count?: number;
  deposit_count?: number;
  error?: string | null;
}

export interface ReconDataStatus {
  stripe: ConnectorInfo;
  netsuite: ConnectorInfo;
}

export function useReconDataStatus() {
  return useQuery<ReconDataStatus>({
    queryKey: ["recon-data-status"],
    queryFn: () => apiClient.get<ReconDataStatus>("/api/v1/reconciliation/data-status"),
  });
}

export function useTriggerReconSync() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiClient.post("/api/v1/reconciliation/sync"),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["recon-data-status"] });
    },
  });
}

export function isStale(lastSync: string | null): boolean {
  if (!lastSync) return true;
  const elapsed = Date.now() - new Date(lastSync).getTime();
  return elapsed > 86400 * 1000; // 24 hours
}
