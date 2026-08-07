"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { FileBarChart, X } from "lucide-react";
import { apiClient } from "@/lib/api-client";
import { Skeleton } from "@/components/ui/skeleton";
import { FreshnessChip } from "@/lib/report-utils";
import { DashboardSwitcher } from "./dashboard-switcher";
import { useDismissDashboardNotice } from "@/hooks/use-dashboard";
import type { DashboardSeriesResponse, DashboardTrackingInfo } from "@/hooks/use-dashboard";
import type { ReportSummary } from "@/hooks/use-reports";

// The frozen artifact is authored at ONE of two fixed inner widths, per
// backend/app/services/report/report_html.py's `report_cls` assembly: the default
// `.report` (840px) for an ordinary report, or `.report--wide` (1120px) ONLY when the
// spec contains a `financial_statement` section. The wall must derive which one a
// given report actually used from its fetched HTML rather than assume — assuming the
// wider 1120 for an 840px (non-wide) report centers its real content inside a too-wide
// frame with dead background on each side and renders it at ~75% of the intended fill
// (840/1120). The wall fits the derived width DOWN on narrow screens but never
// stretches it past 1:1 — the old cropped-card design fixed the preview to a 300px-tall
// window that only ever showed the title band; rendering at full size here retires
// that bug entirely instead of patching the crop height.
const REPORT_WIDTH_NARROW = 840;
const REPORT_WIDTH_WIDE = 1120;
// The exact class token report_html.py's `report_cls` emits on the root <div> only
// when `has_financial_statement` is true (`"report report--wide"` vs plain `"report"`)
// — matching this literal string can't false-positive on unrelated report content,
// which is always HTML-escaped before being embedded.
const WIDE_REPORT_MARKER = 'class="report report--wide"';

function deriveReportWidth(html: string): number {
  return html.includes(WIDE_REPORT_MARKER) ? REPORT_WIDTH_WIDE : REPORT_WIDTH_NARROW;
}

export const WALL_MIN_HEIGHT = 520;

// Rolling-period Stage 1 (Task 5) — the tracking ribbon (mock §3).
//
// "Mon" -> "Month" for the ribbon's prose clauses ("...moves to July the day it
// closes", not "...moves to Jul the day it closes") — periods everywhere else in this
// app are the NetSuite-native "Mon YYYY" abbreviation, but the mock's copy spells the
// bare month out in full. No existing frontend util does this mapping (checked); it's
// small and local enough not to warrant a shared one yet.
const MONTH_FULL: Record<string, string> = {
  Jan: "January",
  Feb: "February",
  Mar: "March",
  Apr: "April",
  May: "May",
  Jun: "June",
  Jul: "July",
  Aug: "August",
  Sep: "September",
  Oct: "October",
  Nov: "November",
  Dec: "December",
};

function fullMonth(period: string): string {
  const abbr = period.split(" ")[0];
  return MONTH_FULL[abbr] ?? abbr;
}

/** "2026-07-24T07:04:00Z" -> "Jul 24" — the grey ribbon's {date}, distinct from
 * report-utils.tsx's `fmtStamp` (date+time, used by FreshnessChip) because the mock's
 * grey copy is deliberately terser ("showing Jun 2026 from Aug 4", no time-of-day). */
function fmtShortDate(iso: string): string {
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** The ribbon above the wall (mock §3) — rendered only for a tracking selection
 * (`tracking` is undefined/null for a plain snapshot; DashboardWall's caller passes
 * `DashboardResponse.active_tracking` straight through, which is itself already gated
 * on that exact condition server-side — see use-dashboard.ts's docstring).
 *
 * Three copy states, verbatim from the approved mock, each gated on the narrowest
 * condition that can only be true for that state — never an `else` catch-all, so a
 * state this component doesn't yet know how to word renders NOTHING rather than a
 * misleading or dishonest fallback (see the two comments below for the two cases that
 * deliberately fall through to null).
 */
function TrackingRibbon({ tracking, report }: { tracking: DashboardTrackingInfo | null | undefined; report: ReportSummary }) {
  // No tracking selection (a pinned snapshot never gets a ribbon — mock §3), or a
  // defensive guard for an empty series (period null): DashboardWall only ever mounts
  // with a real `report` to show, and a null-period tracking shape has no report
  // behind it either (see DashboardTrackingInfo's docstring) — this combination
  // shouldn't reach here in practice, but a ribbon must never crash on it.
  if (!tracking || !tracking.period) return null;
  const period = tracking.period;

  // Amber — forward-compat ONLY. Stage 1's backend never sends `closed_days_ago` (see
  // the field's own doc in use-dashboard.ts); gated on its presence rather than
  // derived by comparing `period`/`resolved_period`, so this starts rendering the day
  // a real backend reports it and never fires on today's traffic.
  if (typeof tracking.closed_days_ago === "number" && tracking.resolved_period) {
    const n = tracking.closed_days_ago;
    return (
      <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border bg-muted/30 px-3 py-2 text-[13px]">
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500" />
        <span className="text-muted-foreground">
          {`${tracking.resolved_period} closed ${n} day${n === 1 ? "" : "s"} ago — building ${fullMonth(tracking.resolved_period)}'s statement now.`}
        </span>
      </div>
    );
  }

  // Green — verified caught up: the live check ran, succeeded, and the truly-last-
  // closed period matches what's on the wall right now.
  if (tracking.period_check_ok && tracking.resolved_period === period) {
    return (
      <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border bg-muted/30 px-3 py-2 text-[13px]">
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" />
        <span className="font-medium text-foreground">{`Last closed period · ${period}`}</span>
        {tracking.next_open_period && (
          <span className="text-muted-foreground">
            {`— ${tracking.next_open_period} is still open in NetSuite. This wall moves to ${fullMonth(tracking.next_open_period)} the day it closes.`}
          </span>
        )}
      </div>
    );
  }

  // Grey — can't tell: `period_check_ok` is false, and (having already excluded the
  // null-period "no report yet" case above) that only ever means the live resolver
  // call ran and degraded — never "no report to check" (see DashboardTrackingInfo's
  // docstring) — so "couldn't reach NetSuite" is always an honest thing to say here.
  // {date} is the report's OWN freshness stamp (last_refreshed_at, falling back to
  // created_at like FreshnessChip) — DashboardTrackingInfo carries no date of its own.
  if (!tracking.period_check_ok) {
    const stampSource = report.last_refreshed_at ?? report.created_at;
    return (
      <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border bg-muted/30 px-3 py-2 text-[13px]">
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-muted-foreground/50" />
        <span className="text-muted-foreground">
          {`Couldn't reach NetSuite to check the close — showing ${period} from ${fmtShortDate(stampSource)}.`}
        </span>
      </div>
    );
  }

  // The check succeeded (period_check_ok=true) but landed on a period OTHER than the
  // one on the wall, and there's no closed_days_ago to word it as amber. Rendering
  // grey's copy here would be false (the check DID reach NetSuite); rendering green's
  // would misdescribe `period` as "the last closed period" when it no longer is. Stage
  // 1 has no sanctioned copy for this in-between state (that's Stage 2's job — see
  // DashboardTrackingInfo's docstring) — showing nothing is the honest choice.
  return null;
}

export interface DashboardWallProps {
  report: ReportSummary;
  /** Rendered directly beneath the header row, above the report display — reserved
   * for the page-level greeting ("Welcome back, {name}"), demoted here per the
   * approved mock (wallpaper-mock-v1 §1). Optional so DashboardWall stays testable
   * standalone without a page-level user/auth dependency. */
  subtitle?: ReactNode;
  /** Task 4: the workspace-wide published set, for the Switch ▾ menu. Always
   * includes `report` itself by construction (the backend never returns an
   * `active` that isn't in `published`). */
  published: ReportSummary[];
  /** Task 4: true when the caller's stored selection is gone (unpublished or
   * deleted) and `report` is the substituted fallback — shows the dismissible
   * one-time notice above the wall. */
  activeIsFallback?: boolean;
  /** Rolling-period Stage 1 (Task 5): the tenant's tracking series, for the Switch ▾
   * menu's "Tracking the close" group (mock §5) — passed straight through to
   * DashboardSwitcher. */
  publishedSeries?: DashboardSeriesResponse[];
  /** Rolling-period Stage 1 (Task 5): present iff `report` is on the wall because of a
   * currently-valid TRACKING selection (`DashboardResponse.active_tracking`) — drives
   * both the ribbon above the display and the TRACKING pill in the header, and tells
   * DashboardSwitcher which series (not report) should carry the switcher's ✓. */
  activeTracking?: DashboardTrackingInfo | null;
}

export function DashboardWall({
  report,
  subtitle,
  published,
  activeIsFallback,
  publishedSeries,
  activeTracking,
}: DashboardWallProps) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [reportWidth, setReportWidth] = useState(REPORT_WIDTH_NARROW);
  const [error, setError] = useState(false);
  // Local state hides the banner immediately on click (no round-trip wait); the
  // dismiss button ALSO fires useDismissDashboardNotice() below so the dismissal
  // persists server-side too — GET is pure (round-3 T2-gate fix) and no longer
  // clears the underlying tombstone as a side effect of a read, so without this
  // explicit call the notice would come back on the next fetch/reload.
  const [bannerDismissed, setBannerDismissed] = useState(false);
  const dismissNotice = useDismissDashboardNotice();
  const containerRef = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState({ width: REPORT_WIDTH_NARROW, height: WALL_MIN_HEIGHT });

  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    setError(false);
    // Clear synchronously so the Skeleton renders during the transition — without
    // this, the previous report's iframe stays on screen under the NEW report's
    // title/freshness chip until this fetch resolves, presenting one report's
    // frozen financials under another report's identity. The previous blob itself
    // is revoked by that prior run's own cleanup below, not here — this reset only
    // touches display state.
    setBlobUrl(null);
    apiClient
      .getText(`/api/v1/reports/${report.id}/view`)
      .then((html) => {
        if (cancelled) return;
        url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
        // Alongside the blob, not before/after it — the iframe width and the scale
        // denominator (below) must always agree with which document is actually loaded.
        setReportWidth(deriveReportWidth(html));
        setBlobUrl((old) => {
          if (old) URL.revokeObjectURL(old); // never leak the previous blob on a switch
          return url;
        });
      })
      .catch(() => !cancelled && setError(true));
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
    // refetch whenever the displayed report itself changes (a switch) OR advances to
    // a new version (auto-refresh) — the wall must never show a stale iframe beside
    // an updated freshness chip.
  }, [report.id, report.version, report.last_refreshed_at]);

  // A dismissal only ever covers the fallback event it was shown for — if the
  // user's NEW pick later gets deleted/unpublished by someone else, that's a
  // distinct fallback event and must not be swallowed by the earlier dismissal.
  useEffect(() => {
    setBannerDismissed(false);
  }, [report.id]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect) setBox({ width: rect.width, height: rect.height });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Fit DOWN on narrow screens; NEVER scale up past 1:1 — the report is authored at a
  // fixed width (840 or 1120, per `reportWidth`) and stretching it past native would
  // blur/misrender it.
  const scale = Math.min(1, box.width / reportWidth);
  // When scale caps at 1 (container wider than reportWidth), the frame no longer fills
  // the container — center the leftover space instead of leaving it all on the right
  // (transformOrigin: "top left" would otherwise pin the frame to the left edge).
  const centerOffset = scale >= 1 ? Math.max(0, (box.width - reportWidth) / 2) : 0;

  const showFallbackNotice = Boolean(activeIsFallback) && !bannerDismissed;

  return (
    <div>
      {showFallbackNotice && (
        <div className="mb-3 flex items-center gap-2.5 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2">
          <span className="flex-1 text-[13px]">
            The dashboard you had chosen is no longer available — showing {report.title} instead.
          </span>
          <button
            type="button"
            aria-label="Dismiss"
            onClick={() => {
              setBannerDismissed(true);
              dismissNotice.mutate();
            }}
            className="shrink-0 text-muted-foreground hover:text-foreground"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <Link
          href={`/reports/${report.id}`}
          className="flex min-w-0 items-center gap-2 hover:opacity-80"
        >
          <FileBarChart className="h-4 w-4 shrink-0 text-primary" />
          <span className="truncate text-[15px] font-semibold text-foreground">
            {report.title}
          </span>
        </Link>
        <FreshnessChip report={report} />
        {/* Rolling-period Stage 1 (Task 5, mock §3): the header pill mirrors the
         * ribbon's own gate (`activeTracking` present) — a snapshot never gets one. */}
        {activeTracking && (
          <span className="shrink-0 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-bold tracking-wide text-primary">
            TRACKING
          </span>
        )}
        <span className="flex-1" />
        <DashboardSwitcher
          published={published}
          activeId={report.id}
          publishedSeries={publishedSeries}
          activeSeriesId={activeTracking?.series_id}
        />
        <Link
          href={`/reports/${report.id}`}
          className="shrink-0 text-[13px] font-medium text-muted-foreground hover:text-foreground"
        >
          Open ↗
        </Link>
      </div>

      {subtitle && <div className="mt-1 mb-3">{subtitle}</div>}

      <TrackingRibbon tracking={activeTracking} report={report} />

      <div
        ref={containerRef}
        className="relative mt-4 overflow-hidden rounded-xl border bg-background"
        style={{ height: "72vh", minHeight: WALL_MIN_HEIGHT }}
      >
        {error ? (
          <div className="flex h-full items-center justify-center text-[13px] text-muted-foreground">
            Preview unavailable
          </div>
        ) : blobUrl ? (
          // sandbox="" is the most restrictive (null origin, no script/forms/popups) —
          // same house standard as the report page's iframe. The report scrolls
          // *inside* this box exactly as it does there: the iframe's rendered height
          // (pre-transform) is sized to the container's visible height divided by the
          // scale, so post-transform it exactly fills the container as a viewport,
          // and the frozen document's own overflow scrolls the rest.
          <iframe
            src={blobUrl}
            title={report.title}
            sandbox=""
            className="border-0"
            style={{
              width: reportWidth,
              height: scale > 0 ? box.height / scale : box.height,
              transform: `scale(${scale})`,
              transformOrigin: "top left",
              marginLeft: `${centerOffset}px`,
            }}
          />
        ) : (
          <Skeleton className="h-full w-full rounded-none" />
        )}
      </div>
    </div>
  );
}

/** House Skeleton at the wall's dimensions — shown while the outer `useDashboard()`
 * query itself is still resolving (before we even know which report is active), so
 * the page doesn't jump once it does. */
export function DashboardWallSkeleton() {
  return (
    <div>
      <div className="flex items-center gap-3">
        <Skeleton className="h-5 w-56 rounded" />
        <Skeleton className="h-5 w-32 rounded-full" />
      </div>
      <Skeleton
        className="mt-4 rounded-xl"
        style={{ height: "72vh", minHeight: WALL_MIN_HEIGHT }}
      />
    </div>
  );
}
