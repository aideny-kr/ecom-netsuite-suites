"use client";

/**
 * Task 4 — the Scripts view's list pane (spec §3.3): search + kind select,
 * filter chips + integration select, families grouped by kind in the fixed
 * order the mock shows (Hooks → Transforms → Filters → Routers → Mixed →
 * Unattached, a group omitted entirely when it's empty rather than shown
 * with a zero count — same convention as `celigo-integration-page.tsx`'s
 * `groupFlows`), and the "Showing X of Y families" footer once a filter
 * actually narrows the account-wide list.
 *
 * Fully controlled: every filter (`filter`/`kind`/`q`/`integrationId`) is a
 * prop, every change is a callback — this component owns no filter state of
 * its own, so `celigo-scripts-page.tsx` can keep the URL as the single
 * source of truth (a refresh or a pasted link reproduces the exact same
 * view) instead of this list quietly diverging from it.
 *
 * `useCeligoIntegrations()` is called HERE, not passed in: the family
 * summary carries only `integration_ids` (spec §2.2 — the list response is
 * deliberately light, no `content`/`content_hash`, and never grew integration
 * NAMES either), so resolving an id to the name the "Integration: any ▾"
 * select shows needs the integrations list this surface already fetches
 * elsewhere. A pending/failed integrations fetch degrades to showing the
 * raw id rather than blocking the whole list on a second query.
 */

import { useEffect, useMemo, useState } from "react";
import { Search } from "lucide-react";
import {
  useCeligoIntegrations,
  type CeligoScriptFamilySummary,
  type CeligoScriptFamilyTotals,
} from "@/hooks/use-celigo-flows";
import type { ScriptsFilter, ScriptsKind } from "../celigo-route";
import { cn } from "@/lib/utils";
import { FamilyRow } from "./family-row";

// ---------------------------------------------------------------------------
// Pure helpers — exported and unit-tested on their own, no rendering needed.
// ---------------------------------------------------------------------------

export type FamilyFilterState = {
  filter: ScriptsFilter;
  kind: ScriptsKind | null;
  q: string;
  integrationId: string | null;
};

/** Applies every one of the four filter axes (spec §3.3's chips, kind
 * select, integration select, and search box) in one pass — a family must
 * pass ALL of them, not any. `q` matches `name`, `function_name`, and every
 * entry of `flow_names` (case-insensitive substring), exactly the three
 * fields spec §3.1 item 2 promises search covers; content search is
 * explicitly out of scope (spec §1). */
export function filterFamilies(
  families: CeligoScriptFamilySummary[],
  state: FamilyFilterState,
): CeligoScriptFamilySummary[] {
  const q = state.q.trim().toLowerCase();
  return families.filter((f) => {
    if (state.filter === "attached" && f.sites_count === 0) return false;
    if (state.filter === "unattached" && f.sites_count > 0) return false;
    if (state.filter === "diverged" && !f.content_diverged) return false;
    if (state.filter === "errors" && f.sites_with_open_errors === 0) return false;
    if (state.kind && f.kind !== state.kind) return false;
    if (state.integrationId && !f.integration_ids.includes(state.integrationId)) return false;
    if (q) {
      const haystack = [f.name, f.function_name ?? "", ...f.flow_names].join(" ␟").toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    return true;
  });
}

const GROUP_ORDER = ["hook", "transform", "filter", "router", "mixed", "unattached"] as const;
type GroupKey = (typeof GROUP_ORDER)[number];
const GROUP_TITLE: Record<GroupKey, string> = {
  hook: "Hooks",
  transform: "Transforms",
  filter: "Filters",
  router: "Routers",
  mixed: "Mixed",
  unattached: "Unattached",
};

export type FamilyGroup = { key: GroupKey; label: string; families: CeligoScriptFamilySummary[] };

/** Buckets into the mock's fixed kind order, header text combining the
 * group's title with its counts in one string (matching
 * `celigo-integration-page.tsx`'s `groupFlows` convention) — every OTHER
 * kind counts SITES (how many places these families actually run);
 * Unattached counts SCRIPTS (`copies_count` summed) instead, since its
 * families have zero sites by definition and "0 sites" would read as a
 * sync gap rather than the point of that group. */
export function groupFamiliesByKind(families: CeligoScriptFamilySummary[]): FamilyGroup[] {
  const buckets: Record<GroupKey, CeligoScriptFamilySummary[]> = {
    hook: [],
    transform: [],
    filter: [],
    router: [],
    mixed: [],
    unattached: [],
  };
  for (const f of families) {
    const bucket = buckets[f.kind as GroupKey];
    if (bucket) bucket.push(f);
  }
  return GROUP_ORDER.filter((key) => buckets[key].length > 0).map((key) => {
    const group = buckets[key];
    const familyCount = `${group.length} famil${group.length === 1 ? "y" : "ies"}`;
    if (key === "unattached") {
      const scripts = group.reduce((sum, f) => sum + f.copies_count, 0);
      return {
        key,
        label: `${GROUP_TITLE[key]} · ${scripts} script${scripts === 1 ? "" : "s"} · ${familyCount}`,
        families: group,
      };
    }
    const sites = group.reduce((sum, f) => sum + f.sites_count, 0);
    return {
      key,
      label: `${GROUP_TITLE[key]} · ${sites} site${sites === 1 ? "" : "s"} · ${familyCount}`,
      families: group,
    };
  });
}

// ---------------------------------------------------------------------------
// Chips
// ---------------------------------------------------------------------------

const CHIPS: { value: ScriptsFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "attached", label: "Attached" },
  { value: "unattached", label: "Unattached" },
  { value: "diverged", label: "Diverged" },
  { value: "errors", label: "Errors" },
];

const KIND_OPTIONS: { value: ScriptsKind | ""; label: string }[] = [
  { value: "", label: "Kind: any" },
  { value: "hook", label: "Hook" },
  { value: "transform", label: "Transform" },
  { value: "filter", label: "Filter" },
  { value: "router", label: "Router" },
  { value: "unattached", label: "Unattached" },
];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function CeligoScriptsList({
  families,
  totals,
  selectedKey,
  onSelect,
  filter,
  kind,
  q,
  integrationId,
  onFilterChange,
  onKindChange,
  onQueryChange,
  onIntegrationChange,
}: {
  families: CeligoScriptFamilySummary[];
  totals: CeligoScriptFamilyTotals;
  selectedKey: string | null;
  onSelect: (dedupKey: string) => void;
  filter: ScriptsFilter;
  kind: ScriptsKind | null;
  q: string;
  integrationId: string | null;
  onFilterChange: (filter: ScriptsFilter) => void;
  onKindChange: (kind: ScriptsKind | null) => void;
  onQueryChange: (q: string) => void;
  onIntegrationChange: (integrationId: string | null) => void;
}): JSX.Element {
  const integrationsQuery = useCeligoIntegrations();

  const integrationNameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const integration of integrationsQuery.data ?? []) map.set(integration.id, integration.name);
    return map;
  }, [integrationsQuery.data]);

  // Only integrations a FAMILY actually references — not every integration
  // in the account, most of which have no scripts at all and would just be
  // dead options in this select.
  const knownIntegrationIds = useMemo(() => {
    const ids = new Set<string>();
    for (const f of families) for (const id of f.integration_ids) ids.add(id);
    return Array.from(ids).sort((a, b) =>
      (integrationNameById.get(a) ?? a).localeCompare(integrationNameById.get(b) ?? b),
    );
  }, [families, integrationNameById]);

  const filtered = useMemo(
    () => filterFamilies(families, { filter, kind, q, integrationId }),
    [families, filter, kind, q, integrationId],
  );
  const groups = useMemo(() => groupFamiliesByKind(filtered), [filtered]);
  const flatRows = useMemo(() => groups.flatMap((g) => g.families), [groups]);

  // The row that ArrowUp/Down moves and Enter commits. Seeded from —  and
  // re-synced to — whichever family is the CURRENT real selection
  // (`selectedKey`, the URL's `family=`) so arriving already on a family
  // highlights it, and a click or an Enter-commit (which round-trips through
  // the URL back into `selectedKey`) doesn't fight this local index. Arrow
  // keys move this WITHOUT calling `onSelect` — the row only opens (a real
  // navigation, a new fetch for the detail pane) on Enter or a direct click,
  // not on every arrow tap.
  const [activeIndex, setActiveIndex] = useState<number>(() =>
    flatRows.findIndex((f) => f.dedup_key === selectedKey),
  );
  useEffect(() => {
    setActiveIndex(flatRows.findIndex((f) => f.dedup_key === selectedKey));
    // flatRows is rebuilt (new array identity) on every filter change too,
    // which is exactly when re-deriving the index against the CURRENT
    // filtered set (not the stale one the previous keystroke navigated)
    // is correct — see filterFamilies/groupFamiliesByKind above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedKey, flatRows]);

  function onRowsKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (flatRows.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, flatRows.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const current = flatRows[activeIndex];
      if (current) onSelect(current.dedup_key);
    }
  }

  const isFiltering = filter !== "all" || !!kind || !!q.trim() || !!integrationId;

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="celigo-scripts-list">
      <div className="flex flex-wrap items-center gap-2 border-b px-2.5 py-2">
        <label className="flex min-w-0 flex-1 items-center gap-1.5 rounded-md border bg-muted/40 px-2 py-1 text-[12px]">
          <Search aria-hidden className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          <input
            type="text"
            value={q}
            onChange={(e) => onQueryChange(e.target.value)}
            placeholder="Search scripts, functions, flows"
            className="w-full min-w-0 bg-transparent text-foreground outline-none placeholder:text-muted-foreground"
          />
        </label>
        <select
          aria-label="Filter by kind"
          value={kind ?? ""}
          onChange={(e) => onKindChange((e.target.value || null) as ScriptsKind | null)}
          className="shrink-0 rounded-md border bg-card px-1.5 py-1 text-[11.5px] text-muted-foreground"
        >
          {KIND_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>
      <div className="flex flex-wrap items-center gap-1 border-b px-2.5 py-1.5 text-[11px]">
        {CHIPS.map((chip) => (
          <button
            key={chip.value}
            type="button"
            aria-pressed={filter === chip.value}
            onClick={() => onFilterChange(chip.value)}
            className={cn(
              "rounded-full border px-2 py-0.5 text-muted-foreground transition-colors",
              filter === chip.value && "border-transparent bg-muted text-foreground",
            )}
          >
            {chip.label}
          </button>
        ))}
        <select
          aria-label="Filter by integration"
          value={integrationId ?? ""}
          onChange={(e) => onIntegrationChange(e.target.value || null)}
          className="ml-auto rounded-md border border-dashed bg-card px-1.5 py-0.5 text-[11px] text-muted-foreground"
        >
          <option value="">Integration: any</option>
          {knownIntegrationIds.map((id) => (
            <option key={id} value={id}>
              {integrationNameById.get(id) ?? id}
            </option>
          ))}
        </select>
      </div>
      <div
        role="group"
        aria-label="Script families"
        tabIndex={0}
        onKeyDown={onRowsKeyDown}
        data-testid="celigo-scripts-rows"
        className="flex-1 overflow-auto outline-none"
      >
        {groups.length === 0 ? (
          <p className="p-6 text-center text-[12.5px] text-muted-foreground">
            {families.length === 0
              ? "No production scripts found."
              : `No families match ${q.trim() ? `"${q.trim()}"` : "the current filters"}. Search covers script names, function names, and the flows a script is attached to. Not the code: that is a later slice.`}
          </p>
        ) : (
          groups.map((group) => (
            <div key={group.key}>
              <div
                data-testid="family-group-header"
                className="bg-muted/40 px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground"
              >
                {group.label}
              </div>
              {group.families.map((f) => (
                <FamilyRow
                  key={f.dedup_key}
                  family={f}
                  selected={flatRows[activeIndex] === f}
                  onSelect={onSelect}
                />
              ))}
            </div>
          ))
        )}
      </div>
      {isFiltering && filtered.length !== totals.families && (
        <div className="border-t px-2.5 py-1.5 text-[11.5px] text-muted-foreground">
          Showing {filtered.length} of {totals.families} families
        </div>
      )}
    </div>
  );
}
