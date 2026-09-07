"use client";

/**
 * Task 4 — one row in the Scripts view's list pane (mockup's `.frow`), and
 * the same row Task 6 reuses on the integration page's Scripts tab (hence
 * `compact` — a denser variant with no kind badge/meta line, just name and
 * copies, for a row that already lives under a "this integration" header).
 *
 * A plain `<button>`, not a `role="option"` list item: `aria-pressed` is the
 * one thing a caller (a test, a screen reader) needs to know — "is this the
 * currently selected family" — and every other status chip on this surface
 * (`shared.tsx`'s `Pill`, `celigo-integrations-page.tsx`'s `FilterButton`)
 * already uses the toggle-button pattern rather than a listbox role.
 */

import { cn } from "@/lib/utils";
import type { CeligoScriptFamilySummary } from "@/hooks/use-celigo-flows";

/** Kind badge code + color, in the same order the mockup's CSS assigns one
 * per kind (`.badge.hk/.tr/.fl`), extended with `router`/`mixed` (spec
 * §2.2's full `kind` enum) using the same two-letter convention. Colors
 * intentionally distinct from `shared.tsx`'s `Pill` tones (ok/crit/warn/
 * mute/accent) — a kind is a category, not a status, and reusing e.g.
 * `warn`'s amber here would read as "this family has a problem". */
const KIND_BADGE: Record<CeligoScriptFamilySummary["kind"], { code: string; className: string }> = {
  hook: { code: "HK", className: "border-blue-500/40 bg-blue-500/10 text-blue-700 dark:text-blue-400" },
  transform: {
    code: "TR",
    className: "border-purple-500/40 bg-purple-500/10 text-purple-700 dark:text-purple-400",
  },
  filter: { code: "FL", className: "border-teal-500/40 bg-teal-500/10 text-teal-700 dark:text-teal-400" },
  router: {
    code: "RT",
    className: "border-indigo-500/40 bg-indigo-500/10 text-indigo-700 dark:text-indigo-400",
  },
  mixed: { code: "MX", className: "border-slate-500/40 bg-slate-500/10 text-slate-700 dark:text-slate-400" },
  unattached: { code: "—", className: "border-border bg-muted text-muted-foreground" },
};

/** "×7 · 3 versions" amber when the family diverged; a bare "×N" (neutral)
 * when every copy shares one content hash — the one place on this row that
 * states "these copies are not identical", so it is the only element that
 * ever turns amber (spec §3.3's "copies pill"). */
function CopiesPill({ family }: { family: CeligoScriptFamilySummary }): JSX.Element {
  const text = family.content_diverged
    ? `×${family.copies_count} · ${family.versions_count} version${family.versions_count === 1 ? "" : "s"}`
    : `×${family.copies_count}`;
  return (
    <span
      className={cn(
        "shrink-0 whitespace-nowrap rounded-full border px-1.5 py-px text-[10.5px] tabular-nums",
        family.content_diverged
          ? "border-transparent bg-amber-500/10 text-amber-700 dark:text-amber-400"
          : "border-border text-muted-foreground",
      )}
    >
      {text}
    </span>
  );
}

export function FamilyRow({
  family,
  selected,
  onSelect,
  compact = false,
}: {
  family: CeligoScriptFamilySummary;
  selected: boolean;
  onSelect: (dedupKey: string) => void;
  /** A denser variant for Task 6's integration Scripts tab — no kind badge,
   * no sites/flows meta line, just name and copies against a "this
   * integration" header that already states the kind grouping. */
  compact?: boolean;
}): JSX.Element {
  const badge = KIND_BADGE[family.kind] ?? KIND_BADGE.unattached;
  const subtitle = family.kind === "unattached" ? "no production site" : family.function_name;
  const meta =
    family.sites_count > 0
      ? `${family.sites_count} site${family.sites_count === 1 ? "" : "s"} · ${family.flows_count} flow${family.flows_count === 1 ? "" : "s"}`
      : null;

  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={() => onSelect(family.dedup_key)}
      data-testid="family-row"
      title={family.name}
      className={cn(
        "flex w-full items-center gap-2 border-b px-2.5 text-left text-[12.5px] last:border-b-0",
        compact ? "py-1" : "py-1.5",
        selected && "bg-accent/60",
      )}
    >
      {!compact && (
        <span
          aria-hidden
          className={cn(
            "inline-flex h-[18px] w-[22px] shrink-0 items-center justify-center rounded border text-[10px] font-bold tracking-wide",
            badge.className,
          )}
        >
          {badge.code}
        </span>
      )}
      <span className="min-w-0 flex-1 truncate">
        {family.name}
        {!compact && subtitle && <small className="ml-1.5 text-muted-foreground">{subtitle}</small>}
      </span>
      {family.other_families_with_name > 0 && (
        <span className="shrink-0 whitespace-nowrap text-[11px] text-muted-foreground">
          {family.other_families_with_name} families with this name
        </span>
      )}
      <CopiesPill family={family} />
      {!compact && meta && (
        <span className="shrink-0 whitespace-nowrap text-[11px] tabular-nums text-muted-foreground">{meta}</span>
      )}
    </button>
  );
}
