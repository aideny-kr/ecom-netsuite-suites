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

/** "Resume" (mock state one, the paused row) — clears `paused_at` server-side. */
export function useResumeScheduledJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiClient.post(`/api/v1/schedules/${id}/resume`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scheduled-jobs"] }),
  });
}
