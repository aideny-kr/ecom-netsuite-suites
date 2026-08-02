"use client";

import Link from "next/link";
import { LayoutDashboard } from "lucide-react";
import { Button } from "@/components/ui/button";

/** Task 5's real empty state — replaces Task 3's interim greeting-only placeholder
 * now that this branch has an owner. Rendered when the dashboard query succeeded
 * but nothing is published tenant-wide yet (`published: []`, `active: null`). The
 * outer `isError` case (the query itself failed) is a distinct branch in page.tsx
 * and does not reach here — this is specifically "legitimately empty," not "the
 * fetch broke." */
export function DashboardEmptyState() {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed bg-card py-16 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-muted">
        <LayoutDashboard className="h-6 w-6 text-muted-foreground" />
      </div>
      <h2 className="mt-4 text-[15px] font-medium text-foreground">
        No dashboard on the wall yet
      </h2>
      <p className="mt-1 mb-5 max-w-md text-[13px] text-muted-foreground">
        Compose a report, then choose Publish to dashboard to put it here. You can
        publish several and switch between them anytime.
      </p>
      <Button asChild variant="outline" size="sm">
        <Link href="/reports">Browse reports →</Link>
      </Button>
    </div>
  );
}
