"use client";

import { useState } from "react";
import Link from "next/link";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useSetActiveDashboard } from "@/hooks/use-dashboard";
import type { ReportSummary } from "@/hooks/use-reports";

export interface DashboardSwitcherProps {
  /** The workspace-wide published set (reports.dashboard_pinned_at IS NOT NULL) —
   * always includes the currently displayed report by construction (the backend
   * never returns an `active` that isn't itself in `published`). */
  published: ReportSummary[];
  /** id of the report currently on the wall — gets the ✓ in the list. */
  activeId: string;
}

/** off/undefined render as "snapshot" (matches FreshnessChip's own snapshot
 * naming in report-utils.tsx) — daily/hourly pass through verbatim. */
function autoRefreshMeta(value: ReportSummary["auto_refresh"]): string {
  return !value || value === "off" ? "snapshot" : value;
}

/** `Switch ▾` menu — reuses the house Radix dropdown (see dropdown-menu.tsx);
 * builds nothing new. Always rendered, even for a single published report,
 * because it also carries "Manage published set…". */
export function DashboardSwitcher({ published, activeId }: DashboardSwitcherProps) {
  const setActive = useSetActiveDashboard();
  // Radix closes the menu on selection, so this can't live as ephemeral menu
  // state — the message must still be visible after the DropdownMenuContent
  // itself has unmounted (e.g. a 409 because another tab unpublished the pick).
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="shrink-0 text-[13px] font-medium text-muted-foreground hover:text-foreground"
          >
            Switch ▾
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuGroup>
            <DropdownMenuLabel>Published dashboards</DropdownMenuLabel>
            {published.map((report) => (
              <DropdownMenuItem
                key={report.id}
                disabled={setActive.isPending}
                onClick={() => {
                  // Belt-and-suspenders alongside the `disabled` prop above: Radix
                  // guards pointer/keyboard selection when disabled, but this
                  // handler is the one source of truth that a second click while
                  // pending can never fire a duplicate PUT.
                  if (!setActive.isPending) {
                    setActionMsg(null);
                    setActive.mutate(report.id, {
                      // The backend's detail strings are user-facing (e.g. a 409
                      // "That report isn't published to the dashboard" when
                      // another tab unpublished it) — surface them instead of
                      // failing silently.
                      onError: (e: Error) => setActionMsg(e.message || "Couldn't switch dashboard"),
                    });
                  }
                }}
                className="flex items-center gap-2"
              >
                <span className="w-3.5 shrink-0">{report.id === activeId ? "✓" : ""}</span>
                <span className="min-w-0 flex-1 truncate">{report.title}</span>
                <span className="ml-2 shrink-0 text-[11px] text-muted-foreground">
                  {autoRefreshMeta(report.auto_refresh)}
                </span>
              </DropdownMenuItem>
            ))}
          </DropdownMenuGroup>
          <DropdownMenuSeparator />
          <DropdownMenuItem asChild>
            <Link href="/reports">Manage published set…</Link>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {actionMsg && <span className="text-[13px] text-destructive">{actionMsg}</span>}
    </>
  );
}
