"use client";

import Link from "next/link";
import { useReports } from "@/hooks/use-reports";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { FileBarChart, ChevronRight } from "lucide-react";

export default function ReportsPage() {
  const { data, isLoading } = useReports();

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight">Reports</h2>
        <p className="mt-1 text-[15px] text-muted-foreground">
          Published reports composed from your analytics
        </p>
      </div>

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
    </div>
  );
}
