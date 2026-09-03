"use client";

/**
 * Task 10 — "My integrations" (mockup screen 1): the tile dashboard every
 * other Celigo page hangs off of. Attention-first sort so a scheduled flow
 * that quietly stopped surfaces ahead of a bigger, quieter integration
 * (absence is not success — see `schedule.ts`'s `stallState` docstring),
 * plus the four filters and the list-view toggle the mockup specifies.
 *
 * Read-only surface: nothing here runs, enables, retries or syncs anything.
 * "Open in Celigo ↗" (Task 12) is the only way out to the real thing.
 */

import { useMemo, useState } from "react";
import { LayoutGrid, List as ListIcon } from "lucide-react";
import {
  useCeligoIntegrations,
  useCeligoSyncStatus,
  type CeligoIntegration,
  type CeligoRecordWrite,
} from "@/hooks/use-celigo-flows";
import { queryState, type QueryState } from "@/lib/query-state";
import { cn } from "@/lib/utils";
import { stallState } from "./schedule";
import { ErrorNotice, ErrorPill, Medallions, Pill, formatRelativeTime } from "./shared";
import { useCeligoRoute } from "./celigo-route";
import { CeligoBreadcrumb } from "./celigo-breadcrumb";

/** A stable reference for "no data yet" — `data ?? []` would allocate a new
 * array every render, which defeats the `useMemo`s below (a changed
 * dependency identity on every render is a `useMemo` that never memoizes). */
const NO_INTEGRATIONS: CeligoIntegration[] = [];

// ---------------------------------------------------------------------------
// Pure helpers — exported for tests, and reused by both the tile and the
// list row so "attention" and "the counts line" are computed exactly once.
// ---------------------------------------------------------------------------

/** The three facts that decide an integration's SECOND pill (the schedule
 * fact — see `shared.tsx`'s `SchedulePill` docstring for why error and
 * schedule are two pills, not one adjective) and whether its tile dims.
 * `stalledCount` checks every row in `flow_schedules` against the SYNC
 * timestamp, never the wall clock, via the same `stallState` a single flow
 * uses — an integration's "attention" is just "how many of its own flows
 * would each show a stalled pill". */
export function integrationAttention(
  integration: CeligoIntegration,
  lastSyncedAt: string | null,
): { stalledCount: number; allPaused: boolean; onDemandOnly: boolean } {
  const stalledCount = integration.flow_schedules.filter(
    (fs) =>
      stallState({
        schedule: fs.schedule,
        disabled: fs.disabled,
        lastExecutedAt: fs.last_executed_at,
        lastSyncedAt,
      }).state === "stalled",
  ).length;
  return {
    stalledCount,
    allPaused: integration.flow_count > 0 && integration.flow_count === integration.paused_count,
    onDemandOnly: integration.scheduled_count === 0 && integration.on_demand_count > 0,
  };
}

/** Attention-first: `error_count desc, stalledCount desc, flow_count desc,
 * name` — a smaller integration with something wrong outranks a bigger
 * quiet one, matching the mockup's "sorted attention-first, then by size". */
export function sortIntegrations(
  list: CeligoIntegration[],
  lastSyncedAt: string | null,
): CeligoIntegration[] {
  const withAttention = list.map((integration) => ({
    integration,
    attention: integrationAttention(integration, lastSyncedAt),
  }));
  withAttention.sort((a, b) => {
    if (a.integration.error_count !== b.integration.error_count) {
      return b.integration.error_count - a.integration.error_count;
    }
    if (a.attention.stalledCount !== b.attention.stalledCount) {
      return b.attention.stalledCount - a.attention.stalledCount;
    }
    if (a.integration.flow_count !== b.integration.flow_count) {
      return b.integration.flow_count - a.integration.flow_count;
    }
    return a.integration.name.localeCompare(b.integration.name);
  });
  return withAttention.map((x) => x.integration);
}

/** "20 flows · 9 scheduled · 6 on demand · 5 paused · 94 steps" — a
 * zero-count segment is omitted rather than printed as "0 paused" noise
 * (the mockup's own tiles do this: a fully-scheduled integration never
 * shows "0 paused"). `flow_count`/`step_count` always show; they're the
 * two facts every integration has regardless of schedule mix. */
function countsLine(integration: CeligoIntegration): string {
  const parts = [`${integration.flow_count} flow${integration.flow_count === 1 ? "" : "s"}`];
  if (integration.scheduled_count > 0) parts.push(`${integration.scheduled_count} scheduled`);
  if (integration.on_demand_count > 0) parts.push(`${integration.on_demand_count} on demand`);
  if (integration.paused_count > 0) parts.push(`${integration.paused_count} paused`);
  parts.push(`${integration.step_count} step${integration.step_count === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

type FilterKey = "all" | "errors" | "stalled" | "paused";

function passesFilter(
  integration: CeligoIntegration,
  attention: { stalledCount: number; allPaused: boolean },
  filter: FilterKey,
): boolean {
  switch (filter) {
    case "errors":
      return integration.error_count > 0;
    case "stalled":
      return attention.stalledCount > 0;
    case "paused":
      return attention.allPaused;
    default:
      return true;
  }
}

// ---------------------------------------------------------------------------
// Small presentational pieces
// ---------------------------------------------------------------------------

function AttentionPill({
  attention,
}: {
  attention: { stalledCount: number; allPaused: boolean; onDemandOnly: boolean };
}): JSX.Element {
  if (attention.allPaused) {
    return (
      <Pill tone="mute" dot="hollow">
        all paused
      </Pill>
    );
  }
  if (attention.stalledCount > 0) {
    return (
      <Pill tone="warn" dot="solid">
        stalled? {attention.stalledCount} flow{attention.stalledCount === 1 ? "" : "s"}
      </Pill>
    );
  }
  if (attention.onDemandOnly) {
    return (
      <Pill tone="mute" dot="hollow">
        on demand only
      </Pill>
    );
  }
  return (
    <Pill tone="ok" dot="solid">
      on time
    </Pill>
  );
}

/** "synced 21 min ago · 17:51 UTC", amber past 2 h — the toolbar's own
 * freshness fact, restated in the page header so it travels with whichever
 * screenshot or link someone shares (see `stallState`'s docstring: every
 * "last ran" on this page is as of THIS timestamp, never the wall clock).
 * Takes the SYNC-STATUS QUERY'S OWN `queryState`, not just `lastSyncedAt` —
 * a `null` timestamp means two different things ("confirmed never synced"
 * vs. "don't know yet, the fetch hasn't resolved") and this pill must never
 * collapse them into the same bare "—" (fix round 1, finding 1). */
function SyncPill({ state, lastSyncedAt }: { state: QueryState; lastSyncedAt: string | null }): JSX.Element {
  if (state === "pending") {
    return (
      <Pill tone="mute" dot="hollow">
        <span className="animate-pulse">checking sync status…</span>
      </Pill>
    );
  }
  if (state === "error") {
    return (
      <Pill tone="crit" dot="solid">
        sync status unavailable
      </Pill>
    );
  }
  if (!lastSyncedAt) {
    return (
      <Pill tone="mute" dot="hollow">
        —
      </Pill>
    );
  }
  const then = new Date(lastSyncedAt);
  const staleMs = Date.now() - then.getTime();
  const stale = staleMs > 2 * 60 * 60 * 1000;
  const hh = String(then.getUTCHours()).padStart(2, "0");
  const mm = String(then.getUTCMinutes()).padStart(2, "0");
  return (
    <Pill tone={stale ? "warn" : "ok"} dot="solid">
      synced {formatRelativeTime(lastSyncedAt)} · {hh}:{mm} UTC
    </Pill>
  );
}

/** "writes SALESORDER ×19 customer ×9 …" — the top four write types (the
 * API already orders `writes` count desc, then record_type — see
 * `CeligoRecordWrite`'s docstring), collapsing the rest into one chip rather
 * than letting a long tail crowd the tile. That chip says "custom records"
 * ONLY when every collapsed type is actually `customrecord_*` — otherwise
 * "custom" would be a claim this data doesn't back (a 5th STANDARD type,
 * e.g. `invoice`, collapsed under "custom records" would be a fabricated
 * label of the same shape every other honest-fallback in this surface goes
 * out of its way to avoid — see `fallbackStepTitle`'s docstring). Empty says
 * so outright: a flow that only reads NetSuite and pushes elsewhere is not a
 * gap in the sync, it's a fact about the integration. */
function WritesLine({ writes }: { writes: CeligoRecordWrite[] }): JSX.Element {
  if (writes.length === 0) {
    return <div className="text-[11px] text-muted-foreground">no NetSuite writes</div>;
  }
  const visible = writes.slice(0, 4);
  const overflow = writes.slice(4);
  const allCustom = overflow.length > 0 && overflow.every((w) => w.record_type.startsWith("customrecord_"));
  return (
    <div className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5 text-[11px]">
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground">writes</span>
      {visible.map((w) => (
        <span key={w.record_type} className="font-mono">
          {w.record_type} ×{w.count}
        </span>
      ))}
      {overflow.length > 0 && (
        <span className="font-mono text-muted-foreground">
          +{overflow.length} {allCustom ? `custom record${overflow.length === 1 ? "" : "s"}` : "more"}
        </span>
      )}
    </div>
  );
}

function TileSkeleton(): JSX.Element {
  return (
    <div
      aria-hidden="true"
      className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(290px,1fr))]"
    >
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="h-[132px] animate-pulse rounded-xl border bg-card p-4 shadow-soft" />
      ))}
    </div>
  );
}

function IntegrationTile({
  integration,
  attention,
  lastSyncedAt,
  onClick,
}: {
  integration: CeligoIntegration;
  attention: { stalledCount: number; allPaused: boolean; onDemandOnly: boolean };
  lastSyncedAt: string | null;
  onClick: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      data-state={attention.allPaused ? "all-paused" : undefined}
      className={cn(
        "flex w-full flex-col gap-1.5 rounded-xl border bg-card p-4 text-left shadow-soft transition-shadow hover:shadow-md",
        attention.allPaused && "opacity-70",
      )}
    >
      <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
        <ErrorPill count={integration.error_count} checkedAt={lastSyncedAt} />
        <AttentionPill attention={attention} />
        <span className="ml-auto">
          <Medallions families={integration.adaptor_families} />
        </span>
      </div>
      <h4 className="text-[15px] font-semibold">{integration.name}</h4>
      <div className="text-[12px] tabular-nums text-muted-foreground">{countsLine(integration)}</div>
      <WritesLine writes={integration.writes} />
      <div className="mt-0.5 flex flex-wrap gap-2.5 border-t pt-1.5 text-[11px] tabular-nums text-muted-foreground">
        <span>
          last run <b className="font-medium text-foreground">{formatRelativeTime(integration.last_run_at)}</b>
        </span>
        <span>
          <b className="font-medium text-foreground">{integration.script_count}</b> script
          {integration.script_count === 1 ? "" : "s"}
        </span>
        <span>
          changes <b className="font-medium text-foreground">{integration.changes_last_24h}</b>
        </span>
      </div>
    </button>
  );
}

function IntegrationsTable({
  integrations,
  attentionById,
  lastSyncedAt,
  onOpen,
}: {
  integrations: CeligoIntegration[];
  attentionById: Map<string, { stalledCount: number; allPaused: boolean; onDemandOnly: boolean }>;
  lastSyncedAt: string | null;
  onOpen: (id: string) => void;
}): JSX.Element {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full border-collapse text-[12px]">
        <thead>
          <tr className="border-b bg-muted text-left text-[10.5px] uppercase tracking-wide text-muted-foreground">
            <th className="px-2.5 py-1.5 font-medium">Integration</th>
            <th className="px-2.5 py-1.5 font-medium">Apps</th>
            <th className="px-2.5 py-1.5 font-medium">Flows</th>
            <th className="px-2.5 py-1.5 font-medium">Writes</th>
            <th className="px-2.5 py-1.5 font-medium">Errors</th>
            <th className="px-2.5 py-1.5 font-medium">Last run</th>
            <th className="px-2.5 py-1.5 font-medium">Scripts</th>
            <th className="px-2.5 py-1.5 font-medium">Changes</th>
          </tr>
        </thead>
        <tbody>
          {integrations.map((integration) => {
            const attention = attentionById.get(integration.id)!;
            return (
              <tr key={integration.id} className={cn("border-b last:border-0", attention.allPaused && "opacity-70")}>
                <td className="px-2.5 py-1.5 font-medium">
                  <button
                    type="button"
                    className="text-left hover:underline"
                    onClick={() => onOpen(integration.id)}
                  >
                    {integration.name}
                  </button>
                </td>
                <td className="px-2.5 py-1.5">
                  <Medallions families={integration.adaptor_families} />
                </td>
                <td className="px-2.5 py-1.5 tabular-nums">{countsLine(integration)}</td>
                <td className="px-2.5 py-1.5">
                  <WritesLine writes={integration.writes} />
                </td>
                <td className="px-2.5 py-1.5">
                  <ErrorPill count={integration.error_count} checkedAt={lastSyncedAt} />
                </td>
                <td className="px-2.5 py-1.5">
                  {/* Fix round 1, finding 2: the schedule/attention pill —
                      "the one that would catch" a quietly-stalled flow — was
                      missing from the list view entirely, even though the
                      tile view (AttentionPill above) and the mockup's own
                      list-view table both carry it in this exact cell. */}
                  <div className="flex flex-wrap items-center gap-1.5 tabular-nums">
                    <span>{formatRelativeTime(integration.last_run_at)}</span>
                    <AttentionPill attention={attention} />
                  </div>
                </td>
                <td className="px-2.5 py-1.5 tabular-nums">{integration.script_count}</td>
                <td className="px-2.5 py-1.5 tabular-nums">{integration.changes_last_24h}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function FilterButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "rounded-full px-2.5 py-0.5 text-muted-foreground transition-colors",
        active && "border bg-muted text-foreground",
      )}
    >
      {children}
    </button>
  );
}

function FiltersRow({
  filter,
  onFilter,
  counts,
  view,
  onView,
}: {
  filter: FilterKey;
  onFilter: (f: FilterKey) => void;
  counts: { all: number; errors: number; stalled: number; paused: number };
  view: "tiles" | "list";
  onView: (v: "tiles" | "list") => void;
}): JSX.Element {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="flex flex-wrap gap-1 text-[11.5px]">
        <FilterButton active={filter === "all"} onClick={() => onFilter("all")}>
          All {counts.all}
        </FilterButton>
        <FilterButton active={filter === "errors"} onClick={() => onFilter("errors")}>
          Open errors {counts.errors}
        </FilterButton>
        <FilterButton active={filter === "stalled"} onClick={() => onFilter("stalled")}>
          Stalled {counts.stalled}
        </FilterButton>
        <FilterButton active={filter === "paused"} onClick={() => onFilter("paused")}>
          All paused {counts.paused}
        </FilterButton>
      </div>
      <div className="ml-auto flex gap-0.5 rounded-md border p-0.5">
        <button
          type="button"
          aria-pressed={view === "tiles"}
          aria-label="Tile view"
          onClick={() => onView("tiles")}
          className={cn("rounded p-1", view === "tiles" && "bg-muted")}
        >
          <LayoutGrid className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          aria-pressed={view === "list"}
          aria-label="List view"
          onClick={() => onView("list")}
          className={cn("rounded p-1", view === "list" && "bg-muted")}
        >
          <ListIcon className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

/** The Celigo surface's index — every other page (an integration, a flow)
 * is reached by clicking through this one. No hook below is called
 * conditionally: both queries, `useState` and both `useMemo`s run on every
 * render regardless of query state, and the pending/error/empty branches
 * only pick which JSX to show — an early `return` before a hook here would
 * change the hook count between the pending render and the render right
 * after data resolves, which is the bug this comment is here to prevent
 * reintroducing. */
export function CeligoIntegrationsPage(): JSX.Element {
  const integrationsQuery = useCeligoIntegrations();
  const syncStatusQuery = useCeligoSyncStatus();
  const route = useCeligoRoute();
  const [filter, setFilter] = useState<FilterKey>("all");

  const integrationsState = queryState(integrationsQuery);
  // Fix round 1, finding 1: gated through queryState() with its OWN explicit
  // pending/error branches (never inferred from `lastSyncedAt` alone) — a
  // sync-status fetch that hasn't resolved yet must never be read as
  // "confirmed never synced", under-report every flow's stall state to
  // "unknown" while presenting it as settled, or leave the SyncPill
  // indistinguishable from a genuine "never synced" pill. See the body
  // branch below and `SyncPill` for the two places that matters.
  const syncStatusState = queryState(syncStatusQuery);
  const lastSyncedAt = syncStatusState === "success" ? syncStatusQuery.data?.last_synced_at ?? null : null;
  const integrations = integrationsQuery.data ?? NO_INTEGRATIONS;

  const sorted = useMemo(
    () => sortIntegrations(integrations, lastSyncedAt),
    [integrations, lastSyncedAt],
  );
  const attentionById = useMemo(() => {
    const map = new Map<string, ReturnType<typeof integrationAttention>>();
    for (const integration of integrations) map.set(integration.id, integrationAttention(integration, lastSyncedAt));
    return map;
  }, [integrations, lastSyncedAt]);

  const neverSynced =
    integrationsState === "success" &&
    syncStatusState === "success" &&
    integrations.length === 0 &&
    lastSyncedAt === null;

  const totals = integrations.reduce(
    (acc, i) => ({
      flows: acc.flows + i.flow_count,
      steps: acc.steps + i.step_count,
      scripts: acc.scripts + i.script_count,
    }),
    { flows: 0, steps: 0, scripts: 0 },
  );
  const headerLine = neverSynced
    ? "— integrations · — flows · — steps · — scripts · production only"
    : `${integrations.length} integration${integrations.length === 1 ? "" : "s"} · ${totals.flows} flow${
        totals.flows === 1 ? "" : "s"
      } · ${totals.steps} step${totals.steps === 1 ? "" : "s"} · ${totals.scripts} script${
        totals.scripts === 1 ? "" : "s"
      } · production only · sorted attention-first, then by size`;

  const filterCounts = {
    all: integrations.length,
    errors: integrations.filter((i) => i.error_count > 0).length,
    stalled: integrations.filter((i) => attentionById.get(i.id)!.stalledCount > 0).length,
    paused: integrations.filter((i) => attentionById.get(i.id)!.allPaused).length,
  };

  let body: JSX.Element;
  if (integrationsState === "pending") {
    body = (
      <>
        <span className="sr-only">Loading integrations…</span>
        <TileSkeleton />
      </>
    );
  } else if (integrationsState === "error") {
    body = (
      <ErrorNotice message="Couldn't load integrations." onRetry={() => integrationsQuery.refetch()} />
    );
  } else if (syncStatusState === "pending") {
    // Integrations resolved, but every fact this page renders past this
    // point (the "never synced" copy, each tile's stall pill, the filter
    // counts) needs `lastSyncedAt` to be trustworthy. Rendering the tiles
    // now would either assert "0 stalled" with no basis (stallState()
    // returns "unknown" with no sync timestamp, which AttentionPill would
    // then show as a confident "on time") or claim "never synced" for a
    // integrationsQuery that resolved empty by coincidence, not by fact.
    body = (
      <>
        <span className="sr-only">Loading sync status…</span>
        <TileSkeleton />
      </>
    );
  } else if (syncStatusState === "error") {
    body = (
      <ErrorNotice message="Couldn't load sync status." onRetry={() => syncStatusQuery.refetch()} />
    );
  } else if (neverSynced) {
    body = (
      <p className="text-[13px] text-muted-foreground">
        No flows synced yet — run a sync from the connector card in Settings.
      </p>
    );
  } else {
    const filtered = sorted.filter((i) => passesFilter(i, attentionById.get(i.id)!, filter));
    body = (
      <>
        <FiltersRow
          filter={filter}
          onFilter={setFilter}
          counts={filterCounts}
          view={route.view}
          onView={(v) => route.go.integrations(v)}
        />
        {route.view === "list" ? (
          <IntegrationsTable
            integrations={filtered}
            attentionById={attentionById}
            lastSyncedAt={lastSyncedAt}
            onOpen={route.go.integration}
          />
        ) : (
          <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(290px,1fr))]">
            {filtered.map((integration) => (
              <IntegrationTile
                key={integration.id}
                integration={integration}
                attention={attentionById.get(integration.id)!}
                lastSyncedAt={lastSyncedAt}
                onClick={() => route.go.integration(integration.id)}
              />
            ))}
          </div>
        )}
      </>
    );
  }

  return (
    <div className="flex flex-1 min-h-0 flex-col">
      <CeligoBreadcrumb items={[{ label: "My integrations" }]} />
      <div className="flex flex-1 min-h-0 flex-col gap-3 overflow-auto p-4">
        <div className="flex flex-wrap items-end gap-3.5">
          <div>
            <h3 className="text-[20px] font-semibold tracking-tight">My integrations</h3>
            <div className="text-[12px] tabular-nums text-muted-foreground">{headerLine}</div>
          </div>
          <div className="ml-auto">
            <SyncPill state={syncStatusState} lastSyncedAt={lastSyncedAt} />
          </div>
        </div>
        {body}
      </div>
    </div>
  );
}
