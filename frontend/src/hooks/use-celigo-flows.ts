"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

// Task 9 — TanStack Query hooks over Task 8's read-only flow-map endpoints
// (backend/app/api/v1/celigo_flows.py). Types below mirror that module's
// Pydantic response models field-for-field -- see its docstring for why the
// backend keeps them explicit (no `raw_json` leakage): these types must stay
// in lockstep with it by hand, the same discipline, not auto-generated.

/** A flow's schedule, relayed by the API as whatever JSON Celigo sent
 * (`CeligoSchedule` in `celigo_flows.py`). The only shape seen live is a
 * six-field cron STRING (e.g. `? 0 0 6 * *`; 96 of 239 flows); `null` is on
 * demand. `formatSchedule` renders anything else as a generic label -- the
 * API deliberately does not vouch for the shape, so neither does this type. */
export type CeligoSchedule = string | Record<string, unknown> | unknown[] | number | boolean | null;

/** `filter_json`/`mapping_json`'s type on the wire (`JsonValue` on
 * `CeligoFlowStepOut` in `celigo_flows.py`) -- the same reasoning as
 * `CeligoSchedule` above: these are opaque Celigo config relayed as-is, a
 * shape nobody has seen yet must not break the whole flow response. */
export type CeligoJson = Record<string, unknown> | unknown[] | string | number | boolean | null;

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
  schedule: CeligoSchedule;
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
  reference_name: string | null;
  filter_json: CeligoJson;
  mapping_json: CeligoJson;
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
  schedule: CeligoSchedule;
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

/** `null` covers BOTH "no active Celigo connection" and "connected, but no
 * sync has ever completed" identically -- see `CeligoSyncStatusOut`'s
 * docstring (backend/app/api/v1/celigo_flows.py). The stats strip has the
 * same one thing to say either way. */
export interface CeligoSyncStatus {
  last_synced_at: string | null;
}

export function useCeligoSyncStatus() {
  return useQuery<CeligoSyncStatus>({
    queryKey: ["celigo", "sync-status"],
    queryFn: () => apiClient.get<CeligoSyncStatus>("/api/v1/celigo/sync-status"),
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

// ---------------------------------------------------------------------------
// Task 10 — script viewer (mockup screen 04), mirroring
// `CeligoScriptAttachmentSiteOut` / `CeligoScriptOut` field-for-field
// (backend/app/api/v1/celigo_flows.py). See that module's docstrings for why
// `content`/`content_hash`/`name` are THIS row's own values while
// `copies_count`/`attachment_count`/`integration_count`/`content_diverged`
// describe the whole clone family -- `content_diverged` in particular is why
// the script viewer must not assume every clone shares identical source.
// ---------------------------------------------------------------------------

export interface CeligoScriptAttachmentSite {
  flow_id: string;
  flow_name: string;
  integration_id: string;
  flow_step_id: string | null;
  /** 'generator' | 'processor' | null (a router-level script ref has no
   * owning step -- see `CeligoFlowDetailOut.unassigned_attachments`). */
  flow_step_role: string | null;
  flow_step_adaptor_type: string | null;
  /** WHICH clone in the logical group was actually attached at this site --
   * not necessarily the id the caller looked up (clones can diverge). */
  script_celigo_id: string;
  /** Opaque locator string, part of a DB unique key -- render verbatim,
   * never parse, never feed to a JSONPath library. */
  json_path: string;
  function_name: string | null;
  /** Best-effort (fragile path-segment matching) -- not authoritative;
   * prefer `json_path` when showing where a script attaches. */
  site_type: string | null;
}

export interface CeligoScript {
  id: string;
  dedup_key: string;
  name: string;
  content: string | null;
  content_hash: string | null;
  copies_count: number;
  attachment_count: number;
  integration_count: number;
  content_diverged: boolean;
  used_by: CeligoScriptAttachmentSite[];
}

export function useCeligoScript(scriptId: string | undefined) {
  return useQuery<CeligoScript>({
    queryKey: ["celigo", "script", scriptId],
    queryFn: () => apiClient.get<CeligoScript>(`/api/v1/celigo/scripts/${scriptId}`),
    enabled: !!scriptId,
  });
}
