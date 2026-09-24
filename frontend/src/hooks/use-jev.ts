"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

export type JevMode = "live" | "shadow" | "off";

export interface JevStatus {
  /** The workspace's choice. */
  mode: JevMode;
  /** What reconciliation will actually do: capped by the deployment, and off without a key. */
  effective_mode: JevMode;
  deployment_cap: JevMode;
  key_source: "tenant" | "platform" | "none";
  /** Last four characters of the workspace's own key; never set for the platform key. */
  key_hint: string | null;
  problem: string | null;
}

export interface JevTestResult {
  success: boolean;
  key_source: "candidate" | "tenant" | "platform" | "none";
  error: string | null;
}

const STATUS_KEY = ["jev", "status"] as const;
const BASE = "/api/v1/connector-status/jev";

export function useJevStatus() {
  return useQuery<JevStatus>({
    queryKey: STATUS_KEY,
    queryFn: () => apiClient.get<JevStatus>(BASE),
  });
}

function useStatusMutation<TVars>(mutationFn: (vars: TVars) => Promise<JevStatus>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn,
    // Refetch rather than write the response into the cache: the key is not scoped by
    // workspace, and a response landing after a workspace switch would show the previous
    // workspace's Jev status.
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: STATUS_KEY });
      queryClient.invalidateQueries({ queryKey: ["connections"] });
    },
  });
}

export function useJevSetMode() {
  return useStatusMutation((mode: JevMode) => apiClient.put<JevStatus>(`${BASE}/mode`, { mode }));
}

export function useJevSaveKey() {
  return useStatusMutation((apiKey: string) => apiClient.put<JevStatus>(`${BASE}/key`, { api_key: apiKey }));
}

export function useJevRemoveKey() {
  return useStatusMutation((_: void) => apiClient.delete<JevStatus>(`${BASE}/key`));
}

export function useJevTest() {
  return useMutation({
    mutationFn: (apiKey?: string) => apiClient.post<JevTestResult>(`${BASE}/test`, apiKey ? { api_key: apiKey } : {}),
  });
}
