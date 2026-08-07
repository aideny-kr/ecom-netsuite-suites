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
import type { DashboardSeriesResponse } from "@/hooks/use-dashboard";
import type { ReportSummary } from "@/hooks/use-reports";

export interface DashboardSwitcherProps {
  /** The workspace-wide published set (reports.dashboard_pinned_at IS NOT NULL) —
   * the "Pinned months" group (mock §5). Always includes the currently displayed
   * report by construction WHEN the active selection is itself a report (the backend
   * never returns a report `active` that isn't in `published`) — not necessarily true
   * for a tracking selection, see `activeId` below. */
  published: ReportSummary[];
  /** id of the report currently on the wall — gets the ✓ on a Pinned months item.
   * Meaningless (and never matched) while a tracking selection is active; pass
   * `activeSeriesId` for that case instead. */
  activeId: string;
  /** Rolling-period Stage 1 (Task 5): the tenant's tracking series — the "Tracking the
   * close" group (mock §5), listed BEFORE Pinned months. Omitted entirely (no group
   * header) when empty, matching Pinned months' own "always at least the manage link"
   * baseline being the only guaranteed-present group. */
  publishedSeries?: DashboardSeriesResponse[];
  /** id of the series currently on the wall (from `DashboardResponse.active_tracking`)
   * — gets the ✓ on a Tracking the close item. Null/undefined when the active
   * selection is a report (or there is none), matching backend's own
   * "at most one of report/series is the real selection" invariant. */
  activeSeriesId?: string | null;
}

/** "income_statement" -> "Income Statement". DashboardSeriesResponse only carries the
 * raw `playbook_key` (no display name — that lives in PLAYBOOKS on the backend, behind
 * a separate GET /reports/playbooks the switcher has no reason to also fetch just for
 * a label). Every current playbook key happens to title-case into its own PLAYBOOKS
 * `name` exactly (`income_statement` -> "Income Statement", etc.) — if a future
 * playbook's display name ever diverges from its title-cased key, this humanizer (not
 * the backend contract) is what needs revisiting. */
function humanizePlaybookKey(key: string): string {
  return key
    .split("_")
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/** `Switch ▾` menu — reuses the house Radix dropdown (see dropdown-menu.tsx);
 * builds nothing new. Always rendered, even for a single published report,
 * because it also carries "Manage published set…". */
export function DashboardSwitcher({
  published,
  activeId,
  publishedSeries = [],
  activeSeriesId = null,
}: DashboardSwitcherProps) {
  const setActive = useSetActiveDashboard();
  // Radix closes the menu on selection, so this can't live as ephemeral menu
  // state — the message must still be visible after the DropdownMenuContent
  // itself has unmounted (e.g. a 409 because another tab unpublished the pick).
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  // Belt-and-suspenders alongside the `disabled` prop on each item below: Radix
  // guards pointer/keyboard selection when disabled, but this is the one source of
  // truth that a second click while pending can never fire a duplicate PUT — shared
  // by both groups so a click in either while the other's PUT is in flight also no-ops.
  function select(selection: Parameters<typeof setActive.mutate>[0]) {
    if (setActive.isPending) return;
    setActionMsg(null);
    setActive.mutate(selection, {
      // The backend's detail strings are user-facing (e.g. a 409 "That report
      // isn't published to the dashboard" when another tab unpublished it) —
      // surface them instead of failing silently.
      onError: (e: Error) => setActionMsg(e.message || "Couldn't switch dashboard"),
    });
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            // Bordered, not ghost: this is the feature's primary control (the approved
            // mock draws it as a button) — a plain text link under-sells it.
            className="shrink-0 rounded-lg border bg-card px-3 py-1.5 text-[13px] font-medium text-foreground shadow-soft hover:bg-muted/40"
          >
            Switch ▾
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {publishedSeries.length > 0 && (
            <DropdownMenuGroup>
              <DropdownMenuLabel>Tracking the close</DropdownMenuLabel>
              {publishedSeries.map((s) => (
                <DropdownMenuItem
                  key={s.id}
                  disabled={setActive.isPending}
                  onClick={() => select({ seriesId: s.id })}
                  className="flex items-center gap-2"
                >
                  <span className="w-3.5 shrink-0">{s.id === activeSeriesId ? "✓" : ""}</span>
                  <span className="min-w-0 flex-1 truncate">{humanizePlaybookKey(s.playbook_key)}</span>
                  {/* "—" when the series has no report composed yet — a real,
                   * momentarily empty state (see DashboardSeriesResponse's docstring),
                   * not an error; never blank (a blank meta column reads as broken). */}
                  <span className="ml-2 shrink-0 text-[11px] text-muted-foreground">{s.period ?? "—"}</span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuGroup>
          )}
          <DropdownMenuGroup>
            <DropdownMenuLabel>Pinned months</DropdownMenuLabel>
            {published.map((report) => (
              <DropdownMenuItem
                key={report.id}
                disabled={setActive.isPending}
                onClick={() => select({ reportId: report.id })}
                className="flex items-center gap-2"
              >
                <span className="w-3.5 shrink-0">{report.id === activeId ? "✓" : ""}</span>
                <span className="min-w-0 flex-1 truncate">{report.title}</span>
                {/* Always the literal word "snapshot" (mock §5) — a Pinned months
                 * entry is, by definition, a fixed single-period artifact, regardless
                 * of whether ITS OWN auto_refresh cadence happens to still be sweeping
                 * its numbers. That per-report cadence is a Reports-page concern
                 * (FreshnessChip); the switcher's distinction is tracking vs pinned. */}
                <span className="ml-2 shrink-0 text-[11px] text-muted-foreground">snapshot</span>
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
