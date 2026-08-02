/** Creator-or-admin gate for destructive report actions (delete/pin) — mirrors the
 * backend's `_can_manage` in `backend/app/api/v1/reports.py`. */
export function canManageReport(
  user: { id: string; roles?: string[] } | null | undefined,
  createdBy: string | null | undefined,
): boolean {
  if (!user?.id) return false;
  if (createdBy && createdBy === user.id) return true;
  return Boolean(user.roles?.includes("admin"));
}

export function fmtStamp(iso: string): string {
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** Minimal shape FreshnessChip needs — duck-typed instead of importing ReportSummary
 * so this stays a leaf module (no dependency on hooks/use-reports.ts); any
 * report-like object (dashboard ReportResponse, ReportSummary) satisfies it as-is. */
export interface FreshnessSource {
  created_at: string;
  last_refreshed_at?: string | null;
  has_recipe: boolean;
  auto_refresh?: string;
  refresh_failure_count?: number;
  auto_refresh_paused_at?: string | null;
}

/** Three-state freshness pill — green (healthy auto-refresh), amber (failing/paused),
 * or plain "Snapshot" (no recipe captured, or auto-refresh off). Was duplicated on the
 * retired PinnedReportCard preview and the report page; lives here once now so the
 * dashboard wall and the report page share one definition. */
export function FreshnessChip({ report }: { report: FreshnessSource }) {
  const stampSource = report.last_refreshed_at ?? report.created_at;
  const stamp = fmtStamp(stampSource);
  const isSnapshot = report.auto_refresh === "off" || !report.has_recipe;
  const isFailing = (report.refresh_failure_count ?? 0) > 0 || Boolean(report.auto_refresh_paused_at);

  if (isSnapshot) {
    return <span className="text-[13px] text-muted-foreground">Snapshot · {stamp}</span>;
  }

  if (isFailing) {
    return (
      <span className="flex items-center gap-1.5 text-[13px] text-muted-foreground">
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500" />
        Refresh failing — data as of {stamp}
      </span>
    );
  }

  return (
    <span className="flex items-center gap-1.5 text-[13px] text-muted-foreground">
      <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" />
      Refreshed {report.auto_refresh} · data as of {stamp}
    </span>
  );
}
