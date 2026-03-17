"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

interface ConnectionHealthItem {
  id: string;
  label: string;
  provider: string;
  status: string;
  auth_type: string | null;
  token_expired: boolean;
  last_health_check: string | null;
  tool_count: number | null;
  client_id: string | null;
  restlet_url: string | null;
}

interface ConnectionHealthResponse {
  connections: ConnectionHealthItem[];
  mcp_connectors: ConnectionHealthItem[];
}

export type { ConnectionHealthItem, ConnectionHealthResponse };

export function useConnectionHealth(enabled = true) {
  return useQuery<ConnectionHealthResponse>({
    queryKey: ["connection-health"],
    queryFn: () => apiClient.get<ConnectionHealthResponse>("/api/v1/connections/health"),
    enabled,
    staleTime: 120_000,
    refetchOnWindowFocus: false,
  });
}
