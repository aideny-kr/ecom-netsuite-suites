"use client";

import { Clock } from "lucide-react";
import { DashboardSwitcher, humanizePlaybookKey } from "./dashboard-switcher";
import type { DashboardSeriesResponse, DashboardTrackingInfo } from "@/hooks/use-dashboard";
import type { ReportSummary } from "@/hooks/use-reports";

/** Round-2 T2-gate fix (MAJOR A): a tracking series has NO report yet the moment it's
 * selected -- mode="tracking" get-or-creates the ReportSeries row before its first
 * period ever composes, and DashboardSwitcher deliberately lets you pick such a series
 * (it renders "—" for the period, see DashboardSwitcher's own docstring). Before this
 * component existed, picking one landed on DashboardEmptyState -- visually and
 * textually IDENTICAL to "nothing published at all" -- with the switcher gone (it only
 * mounts inside DashboardWall, which needs a non-null report), so there was no way back
 * except navigating away, and GET /dashboard reproduced it on every load.
 *
 * page.tsx renders this specifically for `active === null && active_tracking != null`;
 * DashboardEmptyState keeps owning the genuinely-nothing-published case
 * (`active_tracking` absent too). Solid border + a different icon/copy on purpose --
 * must not read as the same empty state. */
export function DashboardTrackingEmptyState({
  tracking,
  published,
  publishedSeries,
}: {
  tracking: DashboardTrackingInfo;
  published: ReportSummary[];
  publishedSeries: DashboardSeriesResponse[];
}) {
  const name = humanizePlaybookKey(tracking.playbook_key);
  // Only claim a specific period when the live close-check actually resolved one
  // (mirrors dashboard-wall.tsx's TrackingRibbon gating on period_check_ok) -- never
  // guess a period this state, by construction, doesn't have a report for yet.
  const periodClause =
    tracking.period_check_ok && tracking.resolved_period ? ` for ${tracking.resolved_period}` : "";
  const message = `This series is selected and waiting for its first report${periodClause}. It'll appear here automatically as soon as it's composed.`;

  return (
    <div className="flex flex-col items-center justify-center rounded-xl border bg-card py-16 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-primary/10">
        <Clock className="h-6 w-6 text-primary" />
      </div>
      <h2 className="mt-4 text-[15px] font-medium text-foreground">{`Tracking ${name}`}</h2>
      <p className="mt-1 mb-5 max-w-md text-[13px] text-muted-foreground">{message}</p>
      {/* No active REPORT to anchor a ✓ against here (activeId requires a string) --
       * an empty string never matches a real report.id (ids are always non-empty
       * UUIDs), so nothing false-checks; the series itself carries the ✓ via
       * activeSeriesId. This is the way back the old dead-end state didn't have. */}
      <DashboardSwitcher
        published={published}
        activeId=""
        publishedSeries={publishedSeries}
        activeSeriesId={tracking.series_id}
      />
    </div>
  );
}
