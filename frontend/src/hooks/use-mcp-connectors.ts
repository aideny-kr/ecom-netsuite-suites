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

export function useUpdateMcpClientId() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, client_id }: { id: string; client_id: string }) =>
      apiClient.patch(`/api/v1/mcp-connectors/${id}/client-id`, { client_id }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors"] });
      queryClient.invalidateQueries({ queryKey: ["connection-health"] });
    },
  });
}

// ---------------------------------------------------------------------------
// BigQuery-specific hooks
// ---------------------------------------------------------------------------

interface BigQueryTestPayload {
  project_id: string;
  service_account_json: Record<string, unknown>;
  location?: string;
}

interface BigQueryTestResponse {
  valid: boolean;
  datasets: string[];
  error: string | null;
}

interface BigQueryCreatePayload {
  project_id: string;
  service_account_json: Record<string, unknown>;
  default_dataset?: string;
  location?: string;
}

export function useTestBigQueryConnection() {
  return useMutation({
    mutationFn: (data: BigQueryTestPayload) =>
      apiClient.post<BigQueryTestResponse>("/api/v1/mcp-connectors/bigquery/test", data),
  });
}

export function useCreateBigQueryConnector() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: BigQueryCreatePayload) =>
      apiClient.post("/api/v1/mcp-connectors/bigquery", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors"] });
      queryClient.invalidateQueries({ queryKey: ["connection-health"] });
    },
  });
}

// ---------------------------------------------------------------------------
// BigQuery table selector hooks
// ---------------------------------------------------------------------------

interface BigQuerySchemaDataset {
  dataset_id: string;
  tables: Array<{
    table_id: string;
    columns?: Array<{ name: string; type: string; description: string | null }>;
    selected: boolean;
  }>;
}

interface BigQuerySchemaResponse {
  datasets: BigQuerySchemaDataset[];
  selected_tables: Record<string, string[]>;
}

export function useBigQuerySchema(connectorId: string | null) {
  return useQuery<BigQuerySchemaResponse>({
    queryKey: ["bigquery-schema", connectorId],
    queryFn: () =>
      apiClient.get<BigQuerySchemaResponse>(
        `/api/v1/mcp-connectors/bigquery/${connectorId}/schema`,
      ),
    enabled: !!connectorId,
  });
}

export function useUpdateBigQueryTables() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      connectorId,
      selectedTables,
    }: {
      connectorId: string;
      selectedTables: Record<string, string[]>;
    }) =>
      apiClient.put(
        `/api/v1/mcp-connectors/bigquery/${connectorId}/tables`,
        { selected_tables: selectedTables },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors"] });
      queryClient.invalidateQueries({ queryKey: ["bigquery-schema"] });
    },
  });
}

// ---------------------------------------------------------------------------
// Google Sheets-specific hooks
// ---------------------------------------------------------------------------

interface SheetsTestPayload {
  service_account_json: Record<string, unknown>;
  shared_drive_id?: string;
}

interface SheetsTestResponse {
  valid: boolean;
  error: string | null;
}

interface SheetsCreatePayload {
  service_account_json: Record<string, unknown>;
  label?: string;
  shared_drive_id?: string;
}

export function useTestSheetsConnection() {
  return useMutation({
    mutationFn: (data: SheetsTestPayload) =>
      apiClient.post<SheetsTestResponse>("/api/v1/mcp-connectors/google-sheets/test", data),
  });
}

export function useCreateSheetsConnector() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: SheetsCreatePayload) =>
      apiClient.post("/api/v1/mcp-connectors/google-sheets", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors"] });
      queryClient.invalidateQueries({ queryKey: ["connection-health"] });
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
