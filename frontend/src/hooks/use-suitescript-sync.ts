"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

interface SyncStatus {
  status: "not_started" | "pending" | "in_progress" | "completed" | "failed";
  message?: string;
  last_sync_at: string | null;
  total_files_loaded: number;
  discovered_file_count: number;
  failed_files_count: number;
  error_message: string | null;
  workspace_id?: string;
}

interface SyncTriggerResponse {
  task_id: string;
  status: string;
}

/**
 * Fetch the current SuiteScript sync status.
 * Polls every 3s while sync is in progress.
 */
export function useSuiteScriptSyncStatus() {
  return useQuery<SyncStatus>({
    queryKey: ["suitescript-sync-status"],
    queryFn: () =>
      apiClient.get<SyncStatus>("/api/v1/netsuite/scripts/sync-status"),
    refetchInterval: (query) => {
      const data = query.state.data;
      if (data && data.status === "in_progress") {
        return 3000; // Poll every 3s while syncing
      }
      return false;
    },
  });
}

/**
 * Trigger a SuiteScript file sync.
 * Invalidates the status query so it starts polling.
 */
export function useTriggerSuiteScriptSync() {
  const queryClient = useQueryClient();
  return useMutation<SyncTriggerResponse, Error>({
    mutationFn: () =>
      apiClient.post<SyncTriggerResponse>(
        "/api/v1/netsuite/scripts/sync",
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["suitescript-sync-status"],
      });
    },
  });
}
