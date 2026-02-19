"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

export interface NetSuiteApiLogEntry {
  id: string;
  direction: string;
  method: string;
  url: string;
  response_status: number | null;
  response_time_ms: number | null;
  error_message: string | null;
  source: string | null;
  created_at: string | null;
}

export function useNetSuiteApiLogs(options?: {
  source?: string;
  status?: string;
  limit?: number;
}) {
  const params = new URLSearchParams();
  if (options?.source) params.set("source", options.source);
  if (options?.status) params.set("status", options.status);
  if (options?.limit) params.set("limit", String(options.limit));
  const qs = params.toString();

  return useQuery<NetSuiteApiLogEntry[]>({
    queryKey: ["netsuite-api-logs", options],
    queryFn: () =>
      apiClient.get<NetSuiteApiLogEntry[]>(
        `/api/v1/netsuite/api-logs${qs ? `?${qs}` : ""}`,
      ),
    refetchInterval: 5000,
  });
}
