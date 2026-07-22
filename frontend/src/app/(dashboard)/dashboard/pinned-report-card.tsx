"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { FileBarChart } from "lucide-react";
import { apiClient } from "@/lib/api-client";
import { Skeleton } from "@/components/ui/skeleton";
import { fmtStamp } from "@/lib/report-utils";
import type { ReportSummary } from "@/hooks/use-reports";

// The frozen artifact is authored at a fixed 1120px inner width; scale it down to fit
// the card so it previews faithfully instead of reflowing at a narrower viewport.
const PREVIEW_WIDTH = 1120;
const PREVIEW_HEIGHT = 300;
const REPORT_CREAM = "#fbf9f4";

interface FreshnessChipProps {
  report: ReportSummary;
}

function FreshnessChip({ report }: FreshnessChipProps) {
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

export function PinnedReportCard({ report }: { report: ReportSummary }) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [error, setError] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(1);

  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    apiClient
      .getText(`/api/v1/reports/${report.id}/view`)
      .then((html) => {
        if (cancelled) return;
        url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
        setBlobUrl(url);
      })
      .catch(() => !cancelled && setError(true));
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
    // refetch when the report advances to a new version (auto-refresh) so the preview
    // never shows a stale iframe beside an updated freshness chip.
  }, [report.id, report.version, report.last_refreshed_at]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width;
      if (width) setScale(width / PREVIEW_WIDTH);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return (
    <div className="rounded-xl border bg-card p-5 shadow-soft">
      <Link
        href={`/reports/${report.id}`}
        className="flex items-center gap-3 hover:opacity-80"
      >
        <FileBarChart className="h-4 w-4 shrink-0 text-primary" />
        <span className="flex-1 truncate text-[15px] font-semibold text-foreground">
          {report.title}
        </span>
        <FreshnessChip report={report} />
        <span className="shrink-0 whitespace-nowrap text-[13px] font-medium text-primary">
          Open report →
        </span>
      </Link>

      <div
        ref={containerRef}
        className="relative mt-4 overflow-hidden rounded-lg border bg-background"
        style={{ height: PREVIEW_HEIGHT }}
      >
        {error ? (
          <div className="flex h-full items-center justify-center text-[13px] text-muted-foreground">
            Preview unavailable
          </div>
        ) : blobUrl ? (
          <>
            <iframe
              src={blobUrl}
              title={report.title}
              sandbox=""
              className="w-full border-0"
              style={{
                width: PREVIEW_WIDTH,
                height: PREVIEW_HEIGHT / scale,
                transform: `scale(${scale})`,
                transformOrigin: "top left",
              }}
            />
            <div
              className="pointer-events-none absolute inset-x-0 bottom-0 h-16"
              style={{
                background: `linear-gradient(to bottom, transparent, ${REPORT_CREAM})`,
              }}
            />
            <Link
              href={`/reports/${report.id}`}
              className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border bg-card px-3 py-1 text-[13px] font-medium text-primary shadow-soft hover:shadow-soft-md"
            >
              Open report →
            </Link>
          </>
        ) : (
          <Skeleton className="h-full w-full rounded-none" />
        )}
      </div>
    </div>
  );
}
