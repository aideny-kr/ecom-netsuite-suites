"use client";

/**
 * Scheduled Jobs platform (Slice 2, spec §B6) — the tenant's own `schedules`
 * rows (`GET /api/v1/schedules`, `backend/app/api/v1/schedules.py`), plus the
 * two list-page actions that mutate a row directly ("Run now", "Resume").
 * The platform's system rows (Celery Beat's own sweeps — "Report
 * auto-refresh" in the mock) are a *different* table entirely and already
 * have a hook: `useJobSchedules()` in `./use-jobs`. Don't duplicate it here.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

/** `delivery_json` is a plain `Optional[dict]` server-side (no schema pins
 * its shape — see `backend/app/schemas/schedule.py::ScheduleCreate` /
 * `ScheduleUpdate`). This is the shape the list page's "Delivers to" column
 * understands, chosen to match the mock's own delivery examples (Drive,
 * email, recon run page, in-app report versions). A `delivery_json` that
 * doesn't match any of these keys renders "--" rather than a guess. */
export interface DeliveryJson {
  drive?: { folder?: string };
  email?: { to?: string; count?: number };
  recon?: { label?: string };
  in_app?: { report_title?: string };
}

/** Wire shape of `ScheduleResponse` (`backend/app/schemas/schedule.py`) —
 * every row in the tenant's `schedules` table, legacy (`schedule_type` in
 * sync|report|recon) and Scheduled Job (`schedule_type === "job"`) alike. */
export interface ScheduledJob {
  id: string;
  tenant_id: string;
  name: string;
  schedule_type: string;
  cron_expression: string | null;
  is_active: boolean;
  parameters: Record<string, unknown> | null;
  instruction: string | null;
  plan_status: string | null;
  plan_version: number;
  timezone: string;
  delivery_json: DeliveryJson | null;
  budget_json: Record<string, unknown> | null;
  catch_up: string;
  last_run_at: string | null;
  last_run_status: string | null;
  next_run_at: string | null;
  paused_at: string | null;
  pause_reason: string | null;
  kinds: string[];
  summary_line: string | null;
  /** True when an APPROVED schedule has a recompiled `pending_plan_json`
   * awaiting approval (the instruction was edited since it was last
   * approved) — `plan_status` stays "approved" in that state, so this is
   * the only list-level signal for it (spec §B6's "Needs attention" tile /
   * row indicator; the full `pending_plan_json`/diff is detail-page only,
   * `ScheduleDetailResponse`). */
  has_pending_plan: boolean;
}

export function useScheduledJobs() {
  return useQuery<ScheduledJob[]>({
    queryKey: ["scheduled-jobs"],
    queryFn: () => apiClient.get<ScheduledJob[]>("/api/v1/schedules"),
  });
}

// ---------------------------------------------------------------------------
// Job detail page (Task 6, mock state two) — one schedule's full shape, its
// run history, and every mutation the detail page's panels perform.
// ---------------------------------------------------------------------------

/** One line of `ScheduleDetailResponse.pending_plan_diff`
 * (`backend/app/schemas/schedule.py::DiffLineOut`, produced by
 * `app.services.jobs.compiler.plan_diff`). `"ctx"` is a non-colored
 * context/header line (e.g. "step 1 · bigquery_sql · location filter" in
 * the mock) — never render it add/del-colored. */
export interface DiffLine {
  kind: "add" | "del" | "ctx";
  step: number | null;
  text: string;
}

/** Wire shape of `ScheduleDetailResponse` — everything `ScheduledJob` has,
 * plus the full `plan_json`/`pending_plan_json`/diff a list row never
 * carries (`GET /api/v1/schedules/{id}` only). */
export interface ScheduleDetail extends ScheduledJob {
  plan_json: { steps: PlanStep[] } | null;
  pending_plan_json: { steps: PlanStep[] } | null;
  pending_plan_reason: string | null;
  pending_plan_diff: DiffLine[];
  owner_id: string | null;
}

/** One entry of a compiled plan's `steps` array — `type` is always one of
 * the registry's allow-listed step types (`app.services.jobs.registry`);
 * an unrecognised `type` still renders (see `shared.tsx`'s `describeStep`)
 * rather than crashing the panel — the registry, not this page, is the
 * single place a step type is validated. */
export interface PlanStep {
  id: string;
  type: string;
  params: Record<string, unknown>;
}

/** Wire shape of `ScheduleRunItem` (`GET /api/v1/schedules/{id}/runs`) —
 * one row per `jobs` table entry the schedule's own runs produced. */
export interface ScheduleRun {
  id: string;
  status: string;
  reason: string | null;
  started_at: string | null;
  completed_at: string | null;
  correlation_id: string | null;
  plan_version: number | null;
  attempt: number | null;
  outputs: Record<string, unknown>;
  detail: string | null;
}

/** Wire shape of `ScheduleRunResponse` (`POST .../run`). */
export interface ScheduleRunResult {
  jobs_id: string | null;
  reason: string;
  outputs: Record<string, unknown>;
}

/** `PATCH /schedules/{id}` body — see `ScheduleUpdate`
 * (`backend/app/schemas/schedule.py`). Every field is independently
 * optional; the instruction panel sends only `instruction`, the schedule
 * panel only its own fields, the pending-change panel's Discard button
 * only `discard_pending`. */
export interface ScheduleUpdateBody {
  instruction?: string;
  cron_expression?: string;
  timezone?: string;
  delivery?: Record<string, unknown>;
  budget?: Record<string, unknown>;
  catch_up?: string;
  name?: string;
  discard_pending?: boolean;
}

/** `GET /api/v1/schedules/{id}` — the full detail shape the job-detail page
 * builds every panel from. `enabled: Boolean(id)` guards the route param
 * arriving empty for one render before Next hydrates it. */
export function useScheduledJob(id: string) {
  return useQuery<ScheduleDetail>({
    queryKey: ["scheduled-jobs", id],
    queryFn: () => apiClient.get<ScheduleDetail>(`/api/v1/schedules/${id}`),
    enabled: Boolean(id),
  });
}

/** `GET /api/v1/schedules/{id}/runs` — the Runs panel's table. */
export function useScheduleRuns(id: string) {
  return useQuery<ScheduleRun[]>({
    queryKey: ["scheduled-jobs", id, "runs"],
    queryFn: () => apiClient.get<ScheduleRun[]>(`/api/v1/schedules/${id}/runs`),
    enabled: Boolean(id),
  });
}

/** `PATCH /schedules/{id}` — every panel's own edit (instruction, schedule,
 * delivery) and the pending-change panel's Discard button share this one
 * mutation; the caller decides which fields to send. Invalidates the whole
 * `["scheduled-jobs"]` prefix (list tiles + this detail + its runs all key
 * off it) rather than just this id, since an instruction edit can also flip
 * the list row's `has_pending_plan`. */
export function useUpdateSchedule(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ScheduleUpdateBody) => apiClient.patch<ScheduleDetail>(`/api/v1/schedules/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scheduled-jobs"] }),
  });
}

/** "Approve · use from next run" (mock state two, pending-change panel, and
 * a schedule's first-ever approval). */
export function useApproveSchedule(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiClient.post<ScheduleDetail>(`/api/v1/schedules/${id}/approve`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scheduled-jobs"] }),
  });
}

/** The detail page's own "Run now" (head action, `use_pending: false`) AND
 * the pending-change panel's "Run once with this change"
 * (`use_pending: true`) — same endpoint, the caller picks the flag per
 * `mutate(usePending)`. Kept separate from `useRunScheduleNow` (the list
 * page's fixed `use_pending: false` mutation for an arbitrary row id) since
 * this one is scoped to a single, route-fixed id and needs both flag
 * values. */
export function useRunSchedule(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (usePending: boolean) =>
      apiClient.post<ScheduleRunResult>(`/api/v1/schedules/${id}/run`, { use_pending: usePending }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scheduled-jobs"] }),
  });
}

/** Head action "Pause" (mock state two). */
export function usePauseSchedule(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiClient.post<ScheduleResponseLike>(`/api/v1/schedules/${id}/pause`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scheduled-jobs"] }),
  });
}

/** Head action "Delete" (mock state two, behind a confirm dialog). */
export function useDeleteSchedule(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiClient.delete<void>(`/api/v1/schedules/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scheduled-jobs"] }),
  });
}

/** Minimal shape `usePauseSchedule`'s response is typed as — the mutation's
 * caller (the head actions bar) only needs the invalidation, never the
 * resolved value, but a concrete type is still better than `unknown` for
 * anyone who later does read `.data` off it. */
type ScheduleResponseLike = Pick<ScheduledJob, "id" | "paused_at" | "pause_reason">;

/** "Run now" (mock state one) — always the approved live plan, never the
 * pending one (that's "Run once with this change", a detail-page-only
 * action per spec §B5/§B6; this list page never sends `use_pending: true`). */
export function useRunScheduleNow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiClient.post(`/api/v1/schedules/${id}/run`, { use_pending: false }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scheduled-jobs"] }),
  });
}

// ---------------------------------------------------------------------------
// New job flow (Task 7, mock state three) — the compile-then-approve create
// path, whether started from this page or from the chat's "schedule this".
// ---------------------------------------------------------------------------

/** `POST /api/v1/schedules` body for the compile path (`ScheduleCreate`'s
 * `instruction`-given branch — `backend/app/api/v1/schedules.py`). A 201
 * response is a `ScheduledJob` (list shape, no `plan_json`) already
 * persisted with `plan_status: "pending_approval"`; the new-job page fetches
 * the full `ScheduleDetail` separately (`useScheduledJob`) to read the
 * compiled plan's steps. A 409 means the compiler asked a clarifying
 * question and created NOTHING — the caller re-`mutate()`s with the
 * instruction plus the operator's answer appended. */
export interface ScheduleCreateBody {
  instruction: string;
}

export function useCreateSchedule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ScheduleCreateBody) => apiClient.post<ScheduledJob>("/api/v1/schedules", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scheduled-jobs"] }),
  });
}

/** "Resume" (mock state one, the paused row) — clears `paused_at` server-side. */
export function useResumeScheduledJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiClient.post(`/api/v1/schedules/${id}/resume`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scheduled-jobs"] }),
  });
}
