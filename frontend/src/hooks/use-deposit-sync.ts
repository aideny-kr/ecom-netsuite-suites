"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

interface DepositSyncStatus {
  active: boolean;
  netsuite_connection_id: string | null;
  netsuite_connection_label: string | null;
  status: "active" | "no_connection" | "sync_failed";
  last_sync_at: string | null;
  deposits_count: number;
  error_message: string | null;
}

interface DepositSyncResult {
  status: string;
  records_synced: number;
  records_new: number;
  records_updated: number;
}

export function useDepositSyncStatus() {
  return useQuery<DepositSyncStatus>({
    queryKey: ["connector-status", "netsuite-deposits"],
    queryFn: () =>
      apiClient.get<DepositSyncStatus>("/api/v1/connector-status/netsuite-deposits"),
  });
}

export function useTriggerDepositSync() {
  const queryClient = useQueryClient();
  return useMutation<DepositSyncResult, Error, void>({
    mutationFn: () =>
      apiClient.post<DepositSyncResult>("/api/v1/connector-status/netsuite-deposits/sync"),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["connector-status", "netsuite-deposits"],
      });
    },
  });
}
