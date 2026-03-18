"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

export interface ConnectionAlert {
  id: string;
  connection_type: "rest_api" | "mcp";
  connection_id: string;
  alert_type: string;
  message: string;
  created_at: string;
}

export function useConnectionAlerts() {
  return useQuery<ConnectionAlert[]>({
    queryKey: ["connection-alerts"],
    queryFn: () => apiClient.get("/api/v1/connection-alerts"),
    refetchInterval: 60_000,
  });
}
