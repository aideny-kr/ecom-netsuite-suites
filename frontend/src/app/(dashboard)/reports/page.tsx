"use client";

import { useState } from "react";
import Link from "next/link";
import { useDeleteReport, useReports, type ReportSummary } from "@/hooks/use-reports";
import { PlaybookLauncher } from "./playbook-launcher";
import { DeleteReportDialog, type DeleteReportDialogReport } from "./delete-report-dialog";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { FileBarChart, ChevronRight, Trash2 } from "lucide-react";
import { useAuth } from "@/providers/auth-provider";

function canManage(userId: string | undefined, roles: string[] | undefined, report: ReportSummary): boolean {
  if (!userId) return false;
  if (report.created_by && report.created_by === userId) return true;
  return Boolean(roles?.includes("admin"));
}

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
              {canManage(user?.id, user?.roles, report) && (
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
