"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { McpConnector, McpConnectorTestResponse } from "@/lib/types";

export function useMcpConnectors() {
  return useQuery<McpConnector[]>({
    queryKey: ["mcp-connectors"],
    queryFn: () => apiClient.get<McpConnector[]>("/api/v1/mcp-connectors"),
  });
}

interface CreateMcpConnectorPayload {
  provider: "netsuite_mcp" | "shopify_mcp" | "custom";
  label: string;
  server_url: string;
  auth_type: "bearer" | "api_key" | "none";
  credentials?: Record<string, string>;
}

export function useCreateMcpConnector() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: CreateMcpConnectorPayload) =>
      apiClient.post<McpConnector>("/api/v1/mcp-connectors", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors"] });
    },
  });
}

export function useDeleteMcpConnector() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) =>
      apiClient.delete<void>(`/api/v1/mcp-connectors/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors"] });
    },
  });
}

export function useTestMcpConnector() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) =>
      apiClient.post<McpConnectorTestResponse>(
        `/api/v1/mcp-connectors/${id}/test`,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors"] });
    },
  });
}
