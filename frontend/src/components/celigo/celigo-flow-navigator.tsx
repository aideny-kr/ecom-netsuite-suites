"use client";

/**
 * Task 14 — the flow page's navigator (mockup screen 3's `.rail`/`.nav`):
 * every sibling flow in the same integration, so hopping between flows
 * never goes back up to the integration table first. Starts collapsed as a
 * narrow rail of health dots (⌘B, or the rail's own toggle, expands it to
 * the named list) — `celigo-flow-page.tsx` owns the actual collapsed/
 * expanded boolean (and the real `react-resizable-panels` collapse), this
 * component only renders whichever half of that state it's told to.
 *
 * Read-only: clicking a row navigates (`onSelect`, wired to `route.go.flow`
 * by the page) — nothing here runs, retries, or toggles a flow.
 */

import { cn } from "@/lib/utils";
import type { CeligoFlowSummary } from "@/hooks/use-celigo-flows";
import { stallState, type StallState } from "./schedule";

/** The three-tone vocabulary every status dot on this surface shares
 * (`Pill`/`SchedulePill` in `shared.tsx`, `celigo-command-palette.tsx`'s own
 * `healthTone`) — reproduced here rather than imported because each of
 * those lives in a file that either predates this one or has no reason to
 * export a helper this small; see `shared.tsx`'s top docstring for the same
 * reasoning applied to `formatRelativeTime`/`ErrorNotice`. */
function dotToneClass(state: StallState["state"] | "crit"): string {
  if (state === "crit") return "bg-red-500";
  if (state === "on_time") return "bg-green-500";
  if (state === "stalled") return "bg-amber-500";
  return "bg-muted-foreground/40";
}

function flowStall(flow: CeligoFlowSummary, lastSyncedAt: string | null): StallState {
  return stallState({
    schedule: flow.schedule,
    disabled: flow.disabled,
    lastExecutedAt: flow.last_executed_at,
    lastSyncedAt,
  });
}

/** Open errors outrank schedule health for this dot: a flow running exactly
 * on schedule while quietly failing every run is not "on time" in any sense
 * an operator cares about — see the cross-surface consistency test (Task
 * 18), which pins this against the SAME flow's own error-pill facts
 * (header, table, bubble) so the rail can never show a calmer story than
 * the rest of the page. Schedule tone only applies once the flow has zero
 * open errors. */
function flowDotState(flow: CeligoFlowSummary, lastSyncedAt: string | null): StallState["state"] | "crit" {
  if (flow.error_count > 0) return "crit";
  return flowStall(flow, lastSyncedAt).state;
}

export function CeligoFlowNavigator({
  flows,
  currentFlowId,
  lastSyncedAt,
  collapsed,
  onToggle,
  onSelect,
}: {
  flows: CeligoFlowSummary[];
  currentFlowId: string;
  lastSyncedAt: string | null;
  collapsed: boolean;
  onToggle: () => void;
  onSelect: (id: string) => void;
}): JSX.Element {
  if (collapsed) {
    return (
      <div
        data-testid="celigo-nav-rail"
        title={`Navigator · ${flows.length} flow${flows.length === 1 ? "" : "s"} · ⌘B expands`}
        className="flex h-full flex-col items-center gap-1 border-r bg-card py-2"
      >
        <button
          type="button"
          onClick={onToggle}
          aria-label="Expand navigator"
          className="text-[11px] text-muted-foreground hover:text-foreground"
        >
          ›
        </button>
        <div className="mt-1.5 flex flex-col items-center gap-1.5">
          {flows.map((flow) => {
            const dotState = flowDotState(flow, lastSyncedAt);
            const current = flow.id === currentFlowId;
            return (
              <span
                key={flow.id}
                data-state={dotState}
                data-current={current ? "true" : undefined}
                title={flow.name}
                className={cn(
                  "shrink-0 rounded-full",
                  current ? "h-2 w-2" : "h-1.5 w-1.5",
                  dotToneClass(dotState),
                )}
              />
            );
          })}
        </div>
      </div>
    );
  }

  return (
    <div data-testid="celigo-nav-list" className="flex h-full flex-col border-r bg-card text-[11.5px]">
      <div className="flex items-center gap-1.5 border-b px-2.5 py-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
        <span>Flows</span>
        <span className="ml-auto tabular-nums">{flows.length}</span>
        <button type="button" onClick={onToggle} aria-label="Collapse navigator" className="hover:text-foreground">
          ‹
        </button>
      </div>
      <ul className="flex-1 overflow-y-auto py-1">
        {flows.map((flow) => {
          const dotState = flowDotState(flow, lastSyncedAt);
          const current = flow.id === currentFlowId;
          const paused = flow.disabled === true;
          return (
            <li key={flow.id}>
              <button
                type="button"
                aria-current={current ? "true" : undefined}
                onClick={() => onSelect(flow.id)}
                className={cn(
                  "grid w-full grid-cols-[8px_1fr] items-start gap-1.5 px-2.5 py-1 text-left",
                  current && "bg-accent/60",
                  paused && "opacity-70",
                )}
              >
                <span
                  data-state={dotState}
                  className={cn("mt-1 h-1.5 w-1.5 shrink-0 rounded-full", dotToneClass(dotState))}
                />
                <span className={cn("truncate", paused ? "text-muted-foreground" : "text-foreground")}>
                  {flow.name}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
