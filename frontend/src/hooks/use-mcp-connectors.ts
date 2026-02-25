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
  provider: "netsuite_mcp" | "shopify_mcp" | "stripe_mcp" | "custom";
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

export function useReauthorizeMcpConnector() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (id: string) => {
      const data = await apiClient.post<{ authorize_url: string; state: string }>(
        `/api/v1/mcp-connectors/${id}/reauthorize`,
      );

      // Open OAuth popup
      const popup = window.open(
        data.authorize_url,
        "netsuite_mcp_reauth",
        "width=600,height=700,scrollbars=yes",
      );

      // Wait for popup result
      return new Promise<{ success: boolean }>((resolve, reject) => {
        const timeout = setTimeout(() => {
          window.removeEventListener("message", handler);
          reject(new Error("Authorization timed out"));
        }, 120000);

        function handler(event: MessageEvent) {
          if (
            event.data?.type === "NETSUITE_MCP_AUTH_SUCCESS" ||
            event.data?.type === "NETSUITE_MCP_AUTH_ERROR"
          ) {
            clearTimeout(timeout);
            window.removeEventListener("message", handler);
            if (event.data.type === "NETSUITE_MCP_AUTH_SUCCESS") {
              resolve({ success: true });
            } else {
              reject(new Error(event.data.error || "Authorization failed"));
            }
          }
        }

        window.addEventListener("message", handler);

        // Also check if popup was closed manually
        const pollTimer = setInterval(() => {
          if (popup?.closed) {
            clearInterval(pollTimer);
            clearTimeout(timeout);
            window.removeEventListener("message", handler);
            reject(new Error("Authorization window was closed"));
          }
        }, 1000);
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors"] });
    },
  });
}
