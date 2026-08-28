"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

// Task 9 — TanStack Query hooks over Task 8's read-only flow-map endpoints
// (backend/app/api/v1/celigo_flows.py). Types below mirror that module's
// Pydantic response models field-for-field -- see its docstring for why the
// backend keeps them explicit (no `raw_json` leakage): these types must stay
// in lockstep with it by hand, the same discipline, not auto-generated.

export interface CeligoIntegration {
  id: string;
  celigo_id: string;
  name: string;
  sandbox: boolean | null;
  mode: string | null;
  description: string | null;
  celigo_last_modified: string | null;
}

export interface CeligoFlowSummary {
  id: string;
  celigo_id: string;
  name: string;
  disabled: boolean | null;
  schedule: Record<string, unknown> | null;
  timezone: string | null;
  last_executed_at: string | null;
  /** Raw open-error count (`resolved_at IS NULL AND purged_at IS NULL`). */
  error_count: number;
  /** Open DISTINCT root-cause count -- the plan's deviation 1 lead value. */
  signature_count: number;
}

export interface CeligoAttachment {
  id: string;
  flow_id: string;
  flow_step_id: string | null;
  script_id: string | null;
  script_celigo_id: string;
  function_name: string | null;
  json_path: string;
  site_type: string | null;
}

export interface CeligoFlowStep {
  id: string;
  celigo_id: string;
  /** 'generator' (source/export) | 'processor' (destination/import). */
  role: string;
  router_id: string | null;
  branch_id: string | null;
  branch_key: string;
  sequence: number;
  adaptor_type: string | null;
  connection_celigo_id: string | null;
  filter_json: Record<string, unknown> | null;
  mapping_json: Record<string, unknown> | null;
  proceed_on_failure: boolean | null;
  skip_retries: boolean | null;
  attachments: CeligoAttachment[];
}

export interface CeligoFlowDetail {
  id: string;
  integration_id: string;
  celigo_id: string;
  name: string;
  disabled: boolean | null;
  schedule: Record<string, unknown> | null;
  timezone: string | null;
  last_executed_at: string | null;
  source_id: string | null;
  ai_description_summary: string | null;
  ai_description_detailed: string | null;
  celigo_last_modified: string | null;
  steps: CeligoFlowStep[];
  unassigned_attachments: CeligoAttachment[];
}

export function useCeligoIntegrations() {
  return useQuery<CeligoIntegration[]>({
    queryKey: ["celigo", "integrations"],
    queryFn: () => apiClient.get<CeligoIntegration[]>("/api/v1/celigo/integrations"),
  });
}

function integrationFlowsQuery(integrationId: string) {
  return {
    queryKey: ["celigo", "integration-flows", integrationId] as const,
    queryFn: () =>
      apiClient.get<CeligoFlowSummary[]>(`/api/v1/celigo/integrations/${integrationId}/flows`),
  };
}

export function useCeligoIntegrationFlows(integrationId: string | undefined) {
  return useQuery<CeligoFlowSummary[]>({
    ...integrationFlowsQuery(integrationId ?? ""),
    enabled: !!integrationId,
  });
}

/** Fetches every integration's flows in parallel. The flow map renders every
 * integration's tree at once (mockup spec) -- the stats strip and every
 * lvl1 "N flows / M failing" rollup need all of them up front, not lazily
 * behind a per-integration click. Shares its cache key with
 * `useCeligoIntegrationFlows` for the same id, so navigating into a single
 * integration later never re-fetches what this already loaded. */
export function useCeligoAllFlows(integrationIds: string[]) {
  return useQueries({
    queries: integrationIds.map((id) => integrationFlowsQuery(id)),
  });
}

export function useCeligoFlowDetail(flowId: string | undefined) {
  return useQuery<CeligoFlowDetail>({
    queryKey: ["celigo", "flow", flowId],
    queryFn: () => apiClient.get<CeligoFlowDetail>(`/api/v1/celigo/flows/${flowId}`),
    enabled: !!flowId,
  });
}
