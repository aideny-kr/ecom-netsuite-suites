"use client";

/**
 * Task 4 — the account-wide Scripts view (spec §3.3): the "Celigo ›
 * Scripts" crumb, five stat tiles wired to the `filter=` URL param, the
 * list|detail split as a `react-resizable-panels` group (percent strings —
 * see `celigo-flow-page.tsx`'s docstring on why: v4.6.4 parses a bare
 * number as PIXELS), and every query/empty state (`queryState()`): a
 * pending query is never rendered as empty, an errored one never as
 * loading or a confident "0 scripts", and the "not synced yet" state
 * (spec §3.3's exact copy) renders instead of a zeroed-out page.
 *
 * The detail pane is Task 5's job — this page renders a placeholder for it
 * so the split (and the `family=` selection driving it) has a real second
 * pane to size against, without pre-empting Task 5's own component.
 *
 * Every `go.scripts(...)` call here re-states the WHOLE current scripts
 * param set (`scriptsBase`) before overriding just the one field that
 * changed — `go.scripts` REPLACES rather than merges (see
 * `celigo-route.ts`'s docstring), so a tile click that only means to
 * change `filter` must not silently drop the current `family`/`q`/`kind`.
 */

import {
  Panel,
  Group as PanelGroup,
  Separator as PanelResizeHandle,
} from "react-resizable-panels";
import {
  useCeligoScriptFamilies,
  useCeligoSyncStatus,
  type CeligoScriptFamilyTotals,
} from "@/hooks/use-celigo-flows";
import { queryState, type QueryState } from "@/lib/query-state";
import { cn } from "@/lib/utils";
import { ErrorNotice, Pill, formatRelativeTime } from "../shared";
import { useCeligoRoute, type CeligoRoute, type ScriptsFilter } from "../celigo-route";
import { CeligoBreadcrumb } from "../celigo-breadcrumb";
import { CeligoScriptsList } from "./celigo-scripts-list";

const LIST_DEFAULT_SIZE = "34%";
const LIST_MIN_SIZE = "24%";

/** Everything `go.scripts` needs to re-state the CURRENT scripts view
 * unchanged, so a caller can spread this and override only the one field
 * it means to change (spec §3.1: `go.scripts` replaces the whole param
 * set, it never merges). Only this file builds this object — every
 * `go.scripts` call below goes through it, so "what survives a tile
 * click / a list filter change" is one decision, not one made per call
 * site that can drift. */
function scriptsBase(route: Pick<CeligoRoute, "familyKey" | "copyId" | "scriptsIntegrationId" | "scriptsFilter" | "scriptsKind" | "q" | "compare">) {
  return {
    family: route.familyKey,
    copy: route.copyId,
    in: route.scriptsIntegrationId,
    filter: route.scriptsFilter,
    kind: route.scriptsKind,
    q: route.q,
    compare: route.compare,
  };
}

// ---------------------------------------------------------------------------
// Stat tiles
// ---------------------------------------------------------------------------

function StatTiles({
  totals,
  activeFilter,
  onFilter,
}: {
  totals: CeligoScriptFamilyTotals;
  activeFilter: ScriptsFilter;
  onFilter: (filter: ScriptsFilter) => void;
}): JSX.Element {
  const tiles: { filter: ScriptsFilter; label: string; value: number; sub: string }[] = [
    { filter: "all", label: "Scripts", value: totals.scripts, sub: `${totals.families} families` },
    {
      filter: "attached",
      label: "Attached",
      value: totals.attached_families,
      sub: `${totals.sites} sites · ${totals.flows_with_sites} of ${totals.flows_total} flows`,
    },
    {
      filter: "unattached",
      label: "Unattached",
      value: totals.unattached_families,
      sub: "families, no production site",
    },
    {
      filter: "diverged",
      label: "Diverged",
      value: totals.diverged_families,
      sub: "families whose copies differ",
    },
    {
      filter: "errors",
      label: "Sites with open errors",
      value: totals.sites_with_open_errors,
      sub: `of ${totals.sites} sites`,
    },
  ];
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5" data-testid="celigo-scripts-tiles">
      {tiles.map((t) => (
        <button
          key={t.filter}
          type="button"
          aria-pressed={activeFilter === t.filter}
          onClick={() => onFilter(t.filter)}
          className={cn(
            "flex flex-col items-start gap-0.5 rounded-lg border bg-card px-3 py-2 text-left",
            activeFilter === t.filter && "border-accent bg-accent/10",
            t.filter === "diverged" && t.value > 0 && "border-l-2 border-l-amber-500",
          )}
        >
          <span className="text-[10.5px] uppercase tracking-wide text-muted-foreground">{t.label}</span>
          <span
            className={cn(
              "text-[18px] font-semibold tabular-nums",
              t.filter === "diverged" && t.value > 0 && "text-amber-700 dark:text-amber-400",
            )}
          >
            {t.value}
          </span>
          <span className="text-[11px] text-muted-foreground">{t.sub}</span>
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sync-status pill
// ---------------------------------------------------------------------------

/** Fix round 1, finding 1: spec §3.3 requires the crumb row to carry
 * "synced-ago from sync status" — every sibling Celigo page fetches its own
 * `useCeligoSyncStatus()` and shows this. Deliberately reproduced here
 * (mirroring `celigo-integrations-page.tsx`'s `SyncPill`) rather than
 * imported: that component isn't exported, and this page's own
 * `familiesQuery.data.synced_at` already answers a DIFFERENT question (has
 * THIS families payload ever synced at all — it gates the big "not synced
 * yet" empty state below) from the account-wide sync status this pill
 * reports, so the two must stay visibly separate rather than share one
 * component whose prop shadows which question it's answering.
 * Takes the sync-status QUERY'S OWN `queryState`, not just the timestamp —
 * a `null` here means two different things ("confirmed never synced" vs.
 * "don't know yet, the fetch hasn't resolved") and must never collapse to
 * the same bare "—". */
function ScriptsSyncPill({ state, lastSyncedAt }: { state: QueryState; lastSyncedAt: string | null }): JSX.Element {
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

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

export function CeligoScriptsPage(): JSX.Element {
  const route = useCeligoRoute();
  const familiesQuery = useCeligoScriptFamilies();
  const state = queryState(familiesQuery);
  // Fix round 1, finding 1 — the crumb's own "synced-ago" fact (spec §3.3),
  // independent of `familiesQuery.data.synced_at`'s different question
  // below. Called unconditionally, alongside every other hook here, before
  // any state branch — see `celigo-integrations-page.tsx`'s docstring on
  // why an early return before a hook is the bug to avoid reintroducing.
  const syncStatusQuery = useCeligoSyncStatus();
  const syncStatusState = queryState(syncStatusQuery);
  const lastSyncedAt = syncStatusState === "success" ? syncStatusQuery.data?.last_synced_at ?? null : null;

  let body: JSX.Element | null = null;
  if (state === "pending") {
    body = (
      <>
        <span className="sr-only">Loading scripts…</span>
        <div aria-hidden className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-16 animate-pulse rounded-lg bg-muted" />
          ))}
        </div>
        <div aria-hidden className="min-h-[320px] flex-1 animate-pulse rounded-lg bg-muted" />
      </>
    );
  } else if (state === "error") {
    body = (
      <ErrorNotice
        message="Couldn't load script families."
        onRetry={() => familiesQuery.refetch()}
      />
    );
  } else {
    const data = familiesQuery.data!;
    if (data.synced_at === null) {
      // Spec §3.3's exact copy: a connection that has never completed a
      // sync shows NOTHING built off zeroed totals — a "0 scripts" here
      // would be a confident claim this data cannot back.
      body = (
        <div className="flex flex-1 items-center justify-center p-8 text-center text-[13px] text-muted-foreground">
          Scripts have not been synced yet. Run the Celigo sync from Settings, then come back.
        </div>
      );
    } else if (data.totals.families === 0) {
      body = (
        <div className="flex flex-1 items-center justify-center p-8 text-center text-[13px] text-muted-foreground">
          No production scripts found.
        </div>
      );
    } else {
      const base = scriptsBase(route);
      body = (
        <>
          <StatTiles
            totals={data.totals}
            activeFilter={route.scriptsFilter}
            onFilter={(filter) => route.go.scripts({ ...base, filter })}
          />
          <div className="flex flex-1 min-h-0" data-testid="celigo-scripts-split">
            <PanelGroup id="celigo-scripts-v1" orientation="horizontal" className="flex h-full w-full">
              {/* Percent-string sizes — see this file's top docstring. */}
              <Panel id="celigo-scripts-list-pane" defaultSize={LIST_DEFAULT_SIZE} minSize={LIST_MIN_SIZE}>
                <CeligoScriptsList
                  families={data.families}
                  totals={data.totals}
                  selectedKey={route.familyKey}
                  onSelect={(family) => route.go.scripts({ ...base, family, copy: null })}
                  filter={route.scriptsFilter}
                  kind={route.scriptsKind}
                  q={route.q}
                  integrationId={route.scriptsIntegrationId}
                  onFilterChange={(filter) => route.go.scripts({ ...base, filter })}
                  onKindChange={(kind) => route.go.scripts({ ...base, kind })}
                  onQueryChange={(q) => route.go.scripts({ ...base, q })}
                  onIntegrationChange={(in_) => route.go.scripts({ ...base, in: in_ })}
                />
              </Panel>
              <PanelResizeHandle className="w-px bg-border" />
              <Panel id="celigo-scripts-detail-pane" className="flex-1">
                <div className="flex h-full items-center justify-center p-6 text-center text-[13px] text-muted-foreground">
                  {route.familyKey
                    ? "Family detail view lands in the next slice."
                    : "Select a family on the left to see its source, versions, and where it's used."}
                </div>
              </Panel>
            </PanelGroup>
          </div>
        </>
      );
    }
  }

  return (
    <div data-testid="celigo-scripts-page" className="flex flex-1 min-h-0 flex-col">
      <CeligoBreadcrumb
        items={[{ label: "Celigo", onClick: () => route.go.integrations() }, { label: "Scripts" }]}
      />
      <div className="flex flex-1 min-h-0 flex-col gap-3 overflow-auto p-4">
        <div className="flex flex-wrap items-center gap-2.5">
          <h3 className="text-[20px] font-semibold tracking-tight">Scripts</h3>
          <ScriptsSyncPill state={syncStatusState} lastSyncedAt={lastSyncedAt} />
        </div>
        {body}
      </div>
    </div>
  );
}
