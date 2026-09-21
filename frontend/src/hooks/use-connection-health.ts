"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { useAuth } from "@/providers/auth-provider";

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
  verification_status?: string | null;
  verification_at?: string | null;
  account_identity?: string | null;
  access_scope?: string | null;
  role?: string | null;
  error_reason?: string | null;
}

interface ConnectionHealthResponse {
  connections: ConnectionHealthItem[];
  mcp_connectors: ConnectionHealthItem[];
}

export type { ConnectionHealthItem, ConnectionHealthResponse };

export function useConnectionHealth(enabled = true) {
  const { user } = useAuth();
  return useQuery<ConnectionHealthResponse>({
    queryKey: ["connection-health", user?.tenant_id],
    queryFn: () => apiClient.get<ConnectionHealthResponse>("/api/v1/connections/health"),
    enabled: enabled && !!user?.tenant_id,
    staleTime: 120_000,
    refetchOnWindowFocus: false,
  });
}
