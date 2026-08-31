"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { ReportSummary } from "@/hooks/use-reports";

// ReportSummary's fields are a superset match for the backend's ReportResponse
// (see backend/app/schemas/report.py + dashboard.py's _to_response) — reused as-is
// rather than duplicating a near-identical type for `published`/`active`.
export interface DashboardResponse {
  /** Workspace-wide published set (reports.dashboard_pinned_at IS NOT NULL),
   * newest-published first. */
  published: ReportSummary[];
  /** Rolling-period Stage 1 (Task 5): the tenant's tracking series (mock §5's
   * "Tracking the close" switcher group), alongside `published`. Defaults to `[]`
   * for callers/tests built against the pre-Task-4 response shape. */
  published_series?: DashboardSeriesResponse[];
  /** This user's displayed report: their stored selection if still published, else
   * the most recently published report, else null (nothing published at all). */
  active: ReportSummary | null;
  /** True only when the user HAD a stored selection that is no longer available
   * (unpublished or deleted) and the fallback substituted for it. Drives Task 4's
   * one-time notice — not consumed here. */
  active_is_fallback: boolean;
  /** Rolling-period Stage 1 (Task 5): present iff `active` resolved from a
   * currently-valid TRACKING (series) selection — absent for a report selection, a
   * fallback, or no selection at all (mirrors backend/app/schemas/dashboard.py's
   * DashboardTrackingInfo docstring exactly). Backs the ribbon above the wall. */
  active_tracking?: DashboardTrackingInfo | null;
}

/** A tracking series as a switcher entry (mock §5's "Tracking the close" group).
 * `period` is the newest linked report's own "Mon YYYY" period — the same value the
 * switcher shows as its meta column; `period` and `report_id` are both null only when
 * the series exists (a tracking compose get-or-creates it up front) but has composed
 * no report yet — a real, momentarily empty state, not an error. Mirrors
 * backend/app/schemas/dashboard.py's DashboardSeriesResponse field-for-field. */
export interface DashboardSeriesResponse {
  id: string;
  playbook_key: string;
  period: string | null;
  report_id: string | null;
}

/** Extra ribbon context, present only when `DashboardResponse.active` came from a
 * tracking selection. Mirrors backend/app/schemas/dashboard.py's DashboardTrackingInfo
 * field-for-field — `period` is the ACTIVE report's own period; `resolved_period` /
 * `next_open_period` are the live NetSuite check's result, set only when
 * `period_check_ok`. See dashboard-wall.tsx's ribbon for how these combine into the
 * mock's green/grey copy. */
export interface DashboardTrackingInfo {
  series_id: string;
  playbook_key: string;
  period: string | null;
  period_check_ok: boolean;
  resolved_period: string | null;
  next_open_period: string | null;
  /** Forward-compat only — Stage 1's backend NEVER sends this field (see the
   * DashboardTrackingInfo docstring in backend/app/schemas/dashboard.py: the ribbon's
   * amber "{period} closed {n} days ago — building {month}'s statement now" state
   * is driven by this field, which Stage 2's scheduled compose populates).
   * The backend sends it ONLY when the series is genuinely behind the last closed
   * period AND ROLLING_PERIOD_AUTO_COMPOSE_ENABLED is on — so whenever it is present,
   * a compose really is scheduled and the ribbon's promise is true. The ribbon gates
   * its amber render on this field's PRESENCE and must NEVER derive it by comparing
   * `period`/`resolved_period`: doing so would render "is scheduled" on a deployment
   * with the sweep switched off, which is the Stage 1 false-promise bug returning. */
  closed_days_ago?: number;
}

export function useDashboard() {
  return useQuery<DashboardResponse>({
    queryKey: ["dashboard"],
    queryFn: () => apiClient.get<DashboardResponse>("/api/v1/dashboard"),
  });
}

// --- Task 4: the switcher's mutations --------------------------------------

/** Rolling-period Stage 1 (Task 5): a switcher selection is EITHER a report
 * (snapshot) OR a series (tracking) — mirrors PUT /dashboard/active's own "exactly
 * one of report_id/series_id" contract (backend/app/schemas/dashboard.py's
 * DashboardActiveRequest). A discriminated union, not two optional fields, so the
 * caller can't accidentally pass both or neither. */
export type ActiveDashboardSelection = { reportId: string } | { seriesId: string };

/** Sets the caller's active dashboard selection — either `{ reportId }` (must already
 * be in the published set, backend 409s otherwise — the switcher only ever offers
 * published reports, so that path isn't reachable from the UI) or `{ seriesId }` (a
 * tracking series; always selectable, even before its first report composes). */
export function useSetActiveDashboard() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (selection: ActiveDashboardSelection) =>
      apiClient.put<DashboardResponse>(
        "/api/v1/dashboard/active",
        "reportId" in selection ? { report_id: selection.reportId } : { series_id: selection.seriesId },
      ),
    // onSettled (not onSuccess): a failed PUT — e.g. the 409 when another tab
    // unpublished the target report in the meantime — leaves the switcher's menu
    // built from a now-stale ["dashboard"] cache. Without refetching on the error
    // path too, the stale entry stays offered and every retry just re-errors the
    // same way; refetching drops it from `published` so the menu self-heals.
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

/** Clears the caller's stored selection — the wall falls back to the most
 * recently published report. Not wired into the switcher UI yet (no "clear"
 * menu item in the approved mock); provided per the plan for a future caller. */
export function useClearActiveDashboard() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiClient.delete<DashboardResponse>("/api/v1/dashboard/active"),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

/** Round-3 T2-gate fix: GET /dashboard is pure now (it no longer clears the
 * deleted-report tombstone as a side effect of a read — a visit to /reports,
 * which shares this same ["dashboard"] query key, was silently consuming the
 * user's one-time notice before they ever saw it on /dashboard). This is the
 * explicit user action that actually clears it: DashboardWall's dismiss
 * button calls this IN ADDITION TO hiding the banner locally, so the
 * dismissal persists instead of depending on the read path. */
export function useDismissDashboardNotice() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiClient.post<DashboardResponse>("/api/v1/dashboard/notice/dismiss"),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}
