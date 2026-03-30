"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

interface StripeStatus {
  connected: boolean;
  connection_id: string | null;
  status: "online" | "offline" | "needs_reauth" | "not_configured";
  api_key_hint: string | null;
  last_verified_at: string | null;
  last_sync_at: string | null;
  payouts_count: number;
  payout_lines_count: number;
  error_message: string | null;
}

interface StripeTestResult {
  success: boolean;
  account_name: string | null;
  account_country: string | null;
  error: string | null;
}

export function useStripeStatus() {
  return useQuery<StripeStatus>({
    queryKey: ["connector-status", "stripe"],
    queryFn: () => apiClient.get<StripeStatus>("/api/v1/connector-status/stripe"),
  });
}

export function useTestStripeConnection() {
  return useMutation<StripeTestResult, Error, { api_key: string }>({
    mutationFn: (data) =>
      apiClient.post<StripeTestResult>("/api/v1/connector-status/stripe/test", data),
  });
}

export function useConnectStripe() {
  const queryClient = useQueryClient();
  return useMutation<unknown, Error, { api_key: string; label?: string }>({
    mutationFn: (data) =>
      apiClient.post("/api/v1/connector-status/stripe/connect", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connector-status", "stripe"] });
    },
  });
}

export function useDisconnectStripe() {
  const queryClient = useQueryClient();
  return useMutation<unknown, Error, void>({
    mutationFn: () => apiClient.delete("/api/v1/connector-status/stripe"),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connector-status", "stripe"] });
    },
  });
}

export function useTriggerStripeSync() {
  const queryClient = useQueryClient();
  return useMutation<unknown, Error, string>({
    mutationFn: (connectionId) =>
      apiClient.post(`/api/v1/connections/${connectionId}/sync`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connector-status", "stripe"] });
    },
  });
}
