"use client";

import { useQuery } from "@tanstack/react-query";
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
