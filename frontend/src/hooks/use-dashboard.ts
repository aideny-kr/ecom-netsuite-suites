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
  /** This user's displayed report: their stored selection if still published, else
   * the most recently published report, else null (nothing published at all). */
  active: ReportSummary | null;
  /** True only when the user HAD a stored selection that is no longer available
   * (unpublished or deleted) and the fallback substituted for it. Drives Task 4's
   * one-time notice — not consumed here. */
  active_is_fallback: boolean;
}

export function useDashboard() {
  return useQuery<DashboardResponse>({
    queryKey: ["dashboard"],
    queryFn: () => apiClient.get<DashboardResponse>("/api/v1/dashboard"),
  });
}

// --- Task 4: the switcher's mutations --------------------------------------

/** Sets the caller's active dashboard selection. `reportId` must already be in
 * the published set (backend 409s otherwise — the switcher only ever offers
 * published reports, so that path isn't reachable from the UI). */
export function useSetActiveDashboard() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (reportId: string) =>
      apiClient.put<DashboardResponse>("/api/v1/dashboard/active", { report_id: reportId }),
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
