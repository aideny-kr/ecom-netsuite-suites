"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

export interface CeligoStatus {
  connected: boolean;
  /** Celigo's /v1/tokenInfo returns no account name — null for every real token. */
  account_name: string | null;
  /** Celigo's `_userId`, the only identifier it exposes. */
  account_id: string | null;
  region: string | null;
  status: string | null;
  agent_access: boolean;
}

export function useCeligoStatus() {
  return useQuery<CeligoStatus>({
    queryKey: ["celigo", "status"],
    queryFn: () => apiClient.get<CeligoStatus>("/api/v1/connector-status/celigo"),
  });
}

interface CeligoTestPayload {
  token: string;
  region: string;
}

interface CeligoTestResult {
  ok: boolean;
  account_name: string | null;
  error: string | null;
}

export function useCeligoTest() {
  return useMutation({
    mutationFn: (data: CeligoTestPayload) =>
      apiClient.post<CeligoTestResult>("/api/v1/connector-status/celigo/test", data),
  });
}

interface CeligoConnectPayload {
  token: string;
  region: string;
  label?: string;
  agent_token?: string;
}

export function useCeligoConnect() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: CeligoConnectPayload) =>
      apiClient.post<CeligoStatus>("/api/v1/connector-status/celigo/connect", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["celigo", "status"] });
    },
  });
}

export function useCeligoDisconnect() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: () => apiClient.delete<void>("/api/v1/connector-status/celigo"),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["celigo", "status"] });
    },
  });
}
