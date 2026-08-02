"use client";

import { useState } from "react";
import Link from "next/link";
import { useDeleteReport, useReports, useUnpinReport, type ReportSummary } from "@/hooks/use-reports";
import { useDashboard } from "@/hooks/use-dashboard";
import { PlaybookLauncher } from "./playbook-launcher";
import { DeleteReportDialog, type DeleteReportDialogReport } from "./delete-report-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { FileBarChart, ChevronRight, Trash2 } from "lucide-react";
import { useAuth } from "@/providers/auth-provider";
import { canManageReport, fmtStamp } from "@/lib/report-utils";

export default function ReportsPage() {
  const { data, isLoading } = useReports();
  const { user } = useAuth();
  const [deleteTarget, setDeleteTarget] = useState<DeleteReportDialogReport | null>(null);

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight">Reports</h2>
        <p className="mt-1 text-[15px] text-muted-foreground">
          Published reports composed from your analytics
        </p>
      </div>

      <PlaybookLauncher />

      <PublishedDashboardsSection />

      {isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-16 w-full rounded-xl" />
          <Skeleton className="h-16 w-full rounded-xl" />
          <Skeleton className="h-16 w-full rounded-xl" />
        </div>
      ) : data?.length ? (
        <div className="space-y-2">
          {data.map((report) => (
            <Link
              key={report.id}
              href={`/reports/${report.id}`}
              className="group flex items-center gap-4 rounded-xl border bg-card p-5 shadow-soft transition-colors hover:bg-muted/30"
            >
              <FileBarChart aria-hidden className="h-5 w-5 shrink-0 text-muted-foreground" />
              <div className="min-w-0 flex-1">
                <p className="truncate text-[15px] font-medium text-foreground">
                  {report.title}
                </p>
                <p className="mt-0.5 text-[13px] text-muted-foreground">
                  {new Date(report.created_at).toLocaleString()}
                </p>
              </div>
              <Badge variant="secondary" className="text-[11px] font-medium">
                {report.status}
              </Badge>
              <span className="text-[12px] tabular-nums text-muted-foreground">
                v{report.version}
              </span>
              {canManageReport(user, report.created_by) && (
                <button
                  type="button"
                  aria-label="Delete report"
                  className="shrink-0 rounded-md p-1 text-muted-foreground opacity-0 transition-opacity hover:text-destructive focus-visible:opacity-100 group-hover:opacity-100"
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    setDeleteTarget({ id: report.id, title: report.title, version: report.version });
                  }}
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              )}
              <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
            </Link>
          ))}
        </div>
      ) : (
        <div className="rounded-xl border bg-card p-12 text-center shadow-soft">
          <p className="text-[15px] font-medium text-muted-foreground">
            No reports yet
          </p>
        </div>
      )}

      {deleteTarget && (
        <ListDeleteDialog
          report={deleteTarget}
          onOpenChange={(open) => !open && setDeleteTarget(null)}
          onDeleted={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}

/** Compact list of every workspace-wide published report (reports.dashboard_pinned_at
 * IS NOT NULL), above the report list — reuses Task 3/4's useDashboard() (already
 * tenant-scoped and dashboard_pinned_at-DESC-sorted) rather than re-deriving
 * "published" + "which one is mine" from useReports(); no new endpoint. Only
 * rendered when at least one report is published. */
function PublishedDashboardsSection() {
  const { data } = useDashboard();
  const { user } = useAuth();
  const published = data?.published ?? [];

  if (!published.length) return null;

  return (
    <div className="rounded-xl border bg-card p-5 shadow-soft">
      <h3 className="text-[13px] font-semibold uppercase tracking-wider text-muted-foreground">
        Published dashboards
      </h3>
      <div className="mt-3 space-y-2">
        {published.map((report) => (
          <PublishedDashboardRow
            key={report.id}
            report={report}
            isOnWall={report.id === data?.active?.id}
            canManage={canManageReport(user, report.created_by)}
          />
        ))}
      </div>
    </div>
  );
}

/** One row of the published-dashboards list. Owns its own useUnpinReport(id) —
 * mirrors the ListDeleteDialog pattern below: each row needs a mutation bound to
 * its own report id, which only works as a per-row hook call, not one shared call
 * over the mapped array. */
function PublishedDashboardRow({
  report,
  isOnWall,
  canManage,
}: {
  report: ReportSummary;
  isOnWall: boolean;
  canManage: boolean;
}) {
  const unpin = useUnpinReport(report.id);

  return (
    <div className="flex items-center gap-3 rounded-lg border bg-background px-3 py-2">
      <Link
        href={`/reports/${report.id}`}
        className="min-w-0 flex-1 truncate text-[13px] font-medium text-foreground hover:underline"
      >
        {report.title}
      </Link>
      {report.dashboard_pinned_at && (
        <span className="shrink-0 text-[11px] text-muted-foreground">
          Published {fmtStamp(report.dashboard_pinned_at)}
        </span>
      )}
      <Badge
        variant={isOnWall ? "default" : "secondary"}
        className="shrink-0 text-[10px] font-semibold uppercase tracking-wide"
      >
        {isOnWall ? "On your wall" : "Published"}
      </Badge>
      {/* The pin/unpin endpoints are creator-or-admin gated server-side — mirror
          that here so nobody sees a button that would just 403. */}
      {canManage && (
        <Button
          variant="ghost"
          size="sm"
          disabled={unpin.isPending}
          onClick={() => unpin.mutate()}
        >
          Unpublish
        </Button>
      )}
    </div>
  );
}

/** Owns the useDeleteReport(id) mutation for whichever row is currently targeted. */
function ListDeleteDialog({
  report,
  onOpenChange,
  onDeleted,
}: {
  report: DeleteReportDialogReport;
  onOpenChange: (open: boolean) => void;
  onDeleted: () => void;
}) {
  const deleteMutation = useDeleteReport(report.id);
  return (
    <DeleteReportDialog
      report={report}
      open
      onOpenChange={onOpenChange}
      onDeleted={onDeleted}
      deleteMutation={deleteMutation}
    />
  );
}
