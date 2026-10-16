"use client";

import Link from "next/link";
import { LayoutDashboard } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CommandLaunch } from "@/components/orbital/command-launch";

/** Task 5's real empty state — replaces Task 3's interim greeting-only placeholder
 * now that this branch has an owner. Rendered when the dashboard query succeeded
 * but nothing is published tenant-wide yet (`published: []`, `active: null`). The
 * outer `isError` case (the query itself failed) is a distinct branch in page.tsx
 * and does not reach here — this is specifically "legitimately empty," not "the
 * fetch broke." */
export function DashboardEmptyState() {
  return (
    <div className="space-y-5">
      <CommandLaunch />
      <div className="flex flex-wrap items-center gap-4 rounded-xl border bg-card p-5">
        <LayoutDashboard className="h-5 w-5 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1 basis-60">
          <h2 className="text-[13px] font-medium text-foreground">
            No reports in Command Center yet
          </h2>
          <p className="mt-1 max-w-xl text-[12px] leading-relaxed text-muted-foreground">
            Compose a report, then publish it to Command Center. You can
            publish several and switch between them anytime.
          </p>
        </div>
        <Button asChild variant="outline" size="sm">
          <Link href="/reports">Browse reports →</Link>
        </Button>
      </div>
    </div>
  );
}
