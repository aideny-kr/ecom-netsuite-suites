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

/** One `(record_type, count)` row of a flow's write mix (`CeligoRecordWriteOut`
 * in `celigo_flows.py`) -- every record type actually POSTED from the flow
 * (a lookup export's `record_type` with no `operation` is a read, not a
 * write, and is excluded server-side), ordered by count desc then
 * record_type. */
export interface CeligoRecordWrite {
  record_type: string;
  count: number;
}

/** One row of `CeligoIntegration.flow_schedules` (`CeligoFlowScheduleOut` in
 * `celigo_flows.py`) -- the per-flow detail behind the card's aggregate
 * schedule counts. */
export interface CeligoFlowSchedule {
  id: string;
  name: string;
  disabled: boolean | null;
  schedule: CeligoSchedule;
  last_executed_at: string | null;
}

export interface CeligoIntegration {
  id: string;
  celigo_id: string;
  name: string;
  sandbox: boolean | null;
  mode: string | null;
  description: string | null;
  celigo_last_modified: string | null;
  /** Task 6 -- dashboard summaries, each a grouped query server-side across
   * every integration at once (never N+1) -- see `CeligoIntegrationOut`'s
   * docstring (backend/app/api/v1/celigo_flows.py) for what each one counts
   * and why `scheduled_count + on_demand_count + paused_count ===
   * flow_count` always. */
  flow_count: number;
  scheduled_count: number;
  on_demand_count: number;
  paused_count: number;
  step_count: number;
  router_count: number;
  lookup_count: number;
  script_count: number;
  no_run_count: number;
  error_count: number;
  /** Task 18 -- the integration-wide twin of `CeligoFlowSummary.signature_count`:
   * DISTINCT root causes across every flow in the integration, so the tile's
   * `ErrorPill` reads "10 open · 1 root cause" the same way the flows table and
   * the flow page already do for the same underlying errors, instead of
   * defaulting to "10 open · 10 root causes" (one claim per row) when this
   * field didn't exist. */
  signature_count: number;
  changes_last_24h: number;
  last_run_at: string | null;
  writes: CeligoRecordWrite[];
  adaptor_families: string[];
  flow_schedules: CeligoFlowSchedule[];
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
  /** Task 5 -- topology/script/write aggregates for the flow-list table
   * columns, each a grouped query server-side (never N+1) -- see
   * `CeligoFlowSummaryOut`'s docstring (backend/app/api/v1/celigo_flows.py)
   * for what each one counts. */
  step_count: number;
  router_count: number;
  branch_count: number;
  lookup_count: number;
  script_count: number;
  diverged_family_count: number;
  writes: CeligoRecordWrite[];
  celigo_last_modified: string | null;
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
  /** Script clone-family state (`topology.script_family_facts`) -- null when
   * the attachment's script isn't synced locally, or is a sandbox copy. */
  script_name: string | null;
  script_size_chars: number | null;
  script_copies_count: number | null;
  script_versions_count: number | null;
  script_version_letter: string | null;
  script_content_diverged: boolean | null;
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
  /** Celigo's own vocabulary (`topology.step_kind`). */
  kind: "source" | "lookup" | "destination";
  record_type: string | null;
  operation: string | null;
  search_id: string | null;
  attachments: CeligoAttachment[];
  /** Open (`celigo_error_is_open()`) error count attributed to THIS step. */
  error_count: number;
}

export interface CeligoRouterBranch {
  id: string | null;
  name: string | null;
  rule_count: number;
  next_router_id: string | null;
  order: number;
  declared_step_count: number;
}

export interface CeligoRouter {
  id: string | null;
  name: string | null;
  route_records_to: string | null;
  route_records_using: string | null;
  has_script_slot: boolean;
  branches: CeligoRouterBranch[];
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
  routers: CeligoRouter[];
  /** Celigo's OWN open-error count/timestamp (`raw_json.numOpenError`/
   * `lastErrorAt`) -- distinct from this app's own error tables. */
  celigo_open_error_count: number | null;
  last_error_at: string | null;
  /** This app's OWN open counts (Task 4). `error_count` is the sum of every
   * step's `error_count` above; `signature_count` is DISTINCT root causes
   * across the whole flow (not a per-step sum, which would over-count a
   * signature spanning multiple steps). */
  error_count: number;
  signature_count: number;
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

// ---------------------------------------------------------------------------
// Task 4 -- grouped flow errors (`CeligoFlowErrorGroupOut`/`CeligoFlowErrorsOut`
// in celigo_flows.py), mirrored field-for-field. `CeligoErrorSignature`/
// `CeligoError` mirror `CeligoErrorSignatureOut`/`CeligoErrorOut` the same way.
// ---------------------------------------------------------------------------

export interface CeligoErrorSignature {
  id: string;
  fingerprint: string;
  source: string | null;
  code: string | null;
  sample_message: string | null;
  occurrence_count: number;
  first_seen: string | null;
  last_seen: string | null;
}

export interface CeligoError {
  id: string;
  celigo_id: string;
  flow_id: string | null;
  flow_step_id: string | null;
  trace_key: string | null;
  source: string | null;
  code: string | null;
  message: string | null;
  occurred_at: string | null;
  purge_at: string | null;
  resolved_at: string | null;
  purged_at: string | null;
  retriable: boolean | null;
}

export interface CeligoFlowErrorGroup {
  signature: CeligoErrorSignature | null;
  count: number;
  step_ids: (string | null)[];
  first_seen_at: string | null;
  last_seen_at: string | null;
  retriable: boolean | null;
  purge_at: string | null;
  trace_keys: string[];
  errors: CeligoError[];
}

export interface CeligoFlowErrors {
  flow_id: string;
  status: "open" | "resolved";
  total: number;
  groups: CeligoFlowErrorGroup[];
}

export function useCeligoFlowErrors(flowId: string | undefined, status: "open" | "resolved" = "open") {
  return useQuery<CeligoFlowErrors>({
    queryKey: ["celigo", "flow", flowId, "errors", status],
    queryFn: () => apiClient.get<CeligoFlowErrors>(`/api/v1/celigo/flows/${flowId}/errors?status=${status}`),
    enabled: !!flowId,
  });
}

// ---------------------------------------------------------------------------
// Task 7 -- config-change routes (`CeligoConfigChangeOut` in celigo_flows.py),
// mirrored field-for-field. `object_id` carries no FK (the model has none
// either -- polymorphic over three object kinds), so it is relayed as-is.
// ---------------------------------------------------------------------------

export interface CeligoConfigChange {
  id: string;
  object_kind: string;
  object_id: string | null;
  celigo_id: string;
  field: string;
  old_value: CeligoJson;
  new_value: CeligoJson;
  flow_id: string | null;
  created_at: string;
}

export function useCeligoIntegrationChanges(integrationId: string | undefined) {
  return useQuery<CeligoConfigChange[]>({
    queryKey: ["celigo", "integration", integrationId, "changes"],
    queryFn: () => apiClient.get<CeligoConfigChange[]>(`/api/v1/celigo/integrations/${integrationId}/changes`),
    enabled: !!integrationId,
  });
}

export function useCeligoFlowChanges(flowId: string | undefined) {
  return useQuery<CeligoConfigChange[]>({
    queryKey: ["celigo", "flow", flowId, "changes"],
    queryFn: () => apiClient.get<CeligoConfigChange[]>(`/api/v1/celigo/flows/${flowId}/changes`),
    enabled: !!flowId,
  });
}
