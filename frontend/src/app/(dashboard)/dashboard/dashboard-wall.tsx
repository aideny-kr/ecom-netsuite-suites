"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { FileBarChart, X } from "lucide-react";
import { apiClient } from "@/lib/api-client";
import { Skeleton } from "@/components/ui/skeleton";
import { FreshnessChip } from "@/lib/report-utils";
import { DashboardSwitcher } from "./dashboard-switcher";
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
}

export function DashboardWall({ report, subtitle, published, activeIsFallback }: DashboardWallProps) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [reportWidth, setReportWidth] = useState(REPORT_WIDTH_NARROW);
  const [error, setError] = useState(false);
  // Dismissal is per-session component state, deliberately NOT persisted (no
  // localStorage/query invalidation) — a fresh mount (reload, revisit) shows the
  // notice again until the backend-tracked selection itself changes.
  const [bannerDismissed, setBannerDismissed] = useState(false);
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
            onClick={() => setBannerDismissed(true)}
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
        <span className="flex-1" />
        <DashboardSwitcher published={published} activeId={report.id} />
        <Link
          href={`/reports/${report.id}`}
          className="shrink-0 text-[13px] font-medium text-muted-foreground hover:text-foreground"
        >
          Open ↗
        </Link>
      </div>

      {subtitle && <div className="mt-1 mb-3">{subtitle}</div>}

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
