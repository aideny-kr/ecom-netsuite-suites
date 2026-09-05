"use client";

/**
 * Task 14 — the flow page header (mockup screen 3's `.fhead`): the error/
 * schedule pills, the flow's own name, the "Open in Celigo ↗" / "Copy link"
 * actions, the one-line facts strip, the derived summary, and the AI
 * description block. Everything here is computed straight off `detail` (a
 * single `CeligoFlowDetail`) — this page never asks the backend for a
 * separate "flow facts" payload.
 *
 * Read-only surface: the two actions are the only affordances. "Copy link"
 * copies the current URL (so a pasted link reproduces exactly this flow);
 * "Open in Celigo ↗" is the one way to actually DO anything about this flow
 * — it leaves the app entirely.
 *
 * `CeligoFlowDetail` does not carry a `writes` rollup or a `script_count` /
 * `diverged_family_count` pair the way `CeligoFlowSummary` does (Task 5's
 * aggregates are integration/flow-LIST columns, computed server-side across
 * many flows at once) — for a single flow's own header, the equivalent
 * facts are cheap enough to derive client-side from `detail.steps` /
 * `detail.unassigned_attachments`, so `computeFlowWrites` and
 * `computeScriptStats` below do that. Exported (like `topologyGlyph`/
 * `groupFlows` in `celigo-integration-page.tsx`) so a test can assert what
 * each one computes without mounting the header.
 */

import { useState } from "react";
import type { CeligoAttachment, CeligoFlowDetail, CeligoFlowStep, CeligoRecordWrite } from "@/hooks/use-celigo-flows";
import type { QueryState } from "@/lib/query-state";
import { parseSchedule, stallState } from "./schedule";
import { ErrorNotice, ErrorPill, Pill, SchedulePill, formatRelativeTime, deriveFlowSummary } from "./shared";
import { cn } from "@/lib/utils";

/** Resolution of `detail.source_id` against the flow's siblings (the page
 * computes this — it alone holds the sibling list — and hands the header
 * just the answer). `null` means "not cloned"; `{ resolvedName: null }`
 * means "cloned, but no sibling in this sync carries that `celigo_id`
 * anymore" (the flow it was cloned from has since been deleted/renamed away
 * in Celigo, or lives in a different integration this sync doesn't cover). */
export type ClonedFromInfo = { resolvedName: string | null };

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Longest AI description shown in full without a clamp or a "Show more"
 * toggle. Exported so the tests build fixtures from it instead of a
 * re-hardcoded number. */
export const AI_DESCRIPTION_CLAMP_CHARS = 200;

/** "2 Sep 2026" — day, short month, full year, read off UTC fields so the
 * date shown never depends on the viewer's (or the test runner's) local
 * timezone. Deliberately carries the year (unlike `celigo-integration-
 * page.tsx`'s `formatShortDate`, a same-purpose function for a table
 * column dense enough to drop it) because this is the only place on the
 * flow page this date appears. `null` is "—", matching every other
 * empty-timestamp value on this surface. */
export function formatFullDate(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "—";
  const d = new Date(ms);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

/** "17:51 UTC" — the wall-clock time a run started, read off UTC fields for
 * the same reason `formatFullDate` does. */
export function formatUtcTime(iso: string): string {
  const d = new Date(Date.parse(iso));
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${hh}:${mm} UTC`;
}

/** Whole minutes between two ISO timestamps, `b - a`. Never negative-zero;
 * `null` when either fails to parse, so a caller can fall back to silence
 * rather than print "NaN min". */
function minutesBetween(a: string, b: string): number | null {
  const ta = Date.parse(a);
  const tb = Date.parse(b);
  if (Number.isNaN(ta) || Number.isNaN(tb)) return null;
  return Math.round((tb - ta) / 60_000);
}

/** Every destination step's `(record_type, count)` — the flow's own write
 * mix, mirroring `CeligoRecordWrite`'s shape so the same "×N" rendering
 * convention used everywhere else on this surface applies here too. A
 * lookup step's `record_type` is a READ (Celigo's own distinction, see
 * `CeligoFlowStep`'s docstring on `kind`) and is excluded, same as the
 * backend's aggregate.
 *
 * Ordered by each type's LATEST step sequence, descending — the type this
 * flow touches closest to the end of its own pipeline leads (its ultimate
 * output), earlier/supporting writes follow. This is deliberately NOT the
 * "biggest count first" convention `celigo-integration-page.tsx`'s
 * `headerWritesLine` uses for a cross-flow rollup: there, more volume is
 * more attention-worthy across many flows; here, for one flow read
 * top-to-bottom, "what does this flow deliver" is the more useful lead —
 * an intermediate customer upsert (touched 4 times keeping two lookups in
 * sync) outranking the sales order it ultimately exists to support would
 * read backwards. Ties break alphabetically for a stable order. */
export function computeFlowWrites(steps: CeligoFlowStep[]): CeligoRecordWrite[] {
  const counts = new Map<string, number>();
  const lastSeq = new Map<string, number>();
  for (const step of steps) {
    if (step.kind !== "destination" || !step.record_type || !step.operation) continue;
    counts.set(step.record_type, (counts.get(step.record_type) ?? 0) + 1);
    lastSeq.set(step.record_type, Math.max(lastSeq.get(step.record_type) ?? -1, step.sequence));
  }
  return Array.from(counts.entries())
    .map(([record_type, count]) => ({ record_type, count }))
    .sort((a, b) => (lastSeq.get(b.record_type)! - lastSeq.get(a.record_type)!) || a.record_type.localeCompare(b.record_type));
}

/** "writes salesorder ×2 · customer ×4" (or "no NetSuite writes"). The same
 * top-4-then-collapse convention as `celigo-integration-page.tsx`'s
 * `headerWritesLine` (reproduced, not imported — that function is private
 * to its own file and this is a different input shape), so an integration
 * with many write types and a single flow with many write types read the
 * same way. */
export function formatFlowWritesLine(writes: CeligoRecordWrite[]): string {
  if (writes.length === 0) return "no NetSuite writes";
  const visible = writes.slice(0, 4);
  const overflow = writes.slice(4);
  const allCustom = overflow.length > 0 && overflow.every((w) => w.record_type.startsWith("customrecord_"));
  const parts = visible.map((w) => `${w.record_type} ×${w.count}`);
  if (overflow.length > 0) {
    parts.push(`+${overflow.length} ${allCustom ? `custom record${overflow.length === 1 ? "" : "s"}` : "more"}`);
  }
  return `writes ${parts.join(" · ")}`;
}

/** Distinct scripts attached anywhere in the flow (every step's own
 * `attachments`, plus router-level `unassigned_attachments`), deduped by
 * `script_id` — the SAME script attached at two sites (the mockup's preMap
 * hook, on both branches' final destination) counts once, matching
 * `CeligoFlowSummary.script_count`'s own semantics. `divergedFamilies`
 * counts distinct scripts where `script_content_diverged` is true on ANY
 * attachment site (a family that diverges shows the same `true` on every
 * site it diverges at, per `topology.script_family_facts`, so the first
 * attachment seen decides it). */
export function computeScriptStats(detail: Pick<CeligoFlowDetail, "steps" | "unassigned_attachments">): {
  count: number;
  divergedFamilies: number;
} {
  const diverged = new Map<string, boolean>();
  const visit = (attachments: CeligoAttachment[]) => {
    for (const a of attachments) {
      if (!a.script_id) continue;
      diverged.set(a.script_id, diverged.get(a.script_id) || !!a.script_content_diverged);
    }
  };
  for (const step of detail.steps) visit(step.attachments);
  visit(detail.unassigned_attachments);
  return {
    count: diverged.size,
    divergedFamilies: Array.from(diverged.values()).filter(Boolean).length,
  };
}

/** Both header pills state a fact AS OF THE SYNC — the error pill's zero form
 * carries "checked {relative}", and the schedule pill's whole verdict is a
 * comparison against the sync timestamp. With the sync-status query pending or
 * failed there is no such timestamp, and the pills used to render anyway:
 * "0 open errors · checked —" beside a bare "—" schedule pill, which reads as
 * a settled all-clear (finding: codex flow-canvas #2). This pill replaces both
 * in that case and says which of the two it is, in the same words
 * `celigo-integrations-page.tsx`'s own `SyncPill` uses. */
function SyncStatusPill({ state }: { state: Exclude<QueryState, "success"> }): JSX.Element {
  if (state === "pending") {
    return (
      <Pill tone="mute" dot="hollow">
        <span className="animate-pulse">checking sync status…</span>
      </Pill>
    );
  }
  return (
    <Pill tone="crit" dot="solid">
      sync status unavailable
    </Pill>
  );
}

/** How many routers and branches this flow really has — the DECLARED ones in
 * `detail.routers` plus any router a step names that the declaration never
 * listed. Those exist (the canvas already draws a node for each, finding
 * I4), so counting only the declared list made the header under-report the
 * shape of the very picture below it. Exported for the same reason
 * `computeFlowWrites` is: the count is a rule worth testing without mounting
 * a header.
 *
 * A branch is keyed by `(router id, branch id)`; an id-less branch falls back
 * to its declared `order` so two unnamed branches of one router never
 * collapse into one — the same rule `layout.ts`'s `branchKey` applies. Steps
 * of an undeclared router contribute their own branch ids, with a single
 * "unassigned" bucket for those that name no branch. */
export function computeTopologyCounts(
  detail: Pick<CeligoFlowDetail, "steps" | "routers">,
): { routerCount: number; branchCount: number } {
  const declaredIds = new Set(
    detail.routers.map((r) => r.id).filter((id): id is string => typeof id === "string"),
  );
  const routerIds = new Set(declaredIds);
  for (const step of detail.steps) if (step.router_id) routerIds.add(step.router_id);

  const branchKeys = new Set<string>();
  for (const router of detail.routers) {
    if (!router.id) continue;
    for (const branch of router.branches) branchKeys.add(`${router.id}#${branch.id ?? `order:${branch.order}`}`);
  }
  for (const step of detail.steps) {
    if (!step.router_id || declaredIds.has(step.router_id)) continue;
    branchKeys.add(`${step.router_id}#${step.branch_id ?? "unassigned"}`);
  }

  return { routerCount: routerIds.size, branchCount: branchKeys.size };
}

export function CeligoFlowHeader({
  detail,
  lastSyncedAt,
  syncStatusState,
  onRetrySyncStatus,
  integrationName,
  integrationCeligoId,
  clonedFrom,
  integrationNotice,
  headerCollapsed,
  onToggleHeaderCollapsed,
}: {
  detail: CeligoFlowDetail;
  lastSyncedAt: string | null;
  /** `queryState()` of the sync-status query. `lastSyncedAt` alone cannot
   * carry this: `null` means "confirmed never synced", "still fetching" and
   * "the fetch failed" all at once, and the three must not render alike. */
  syncStatusState: QueryState;
  onRetrySyncStatus: () => void;
  integrationName: string;
  integrationCeligoId: string | null;
  clonedFrom: ClonedFromInfo | null;
  /** Rendered under the pill row when the INTEGRATIONS query failed — the
   * page keeps rendering (the flow itself loaded), but the missing
   * integration name and Open-in-Celigo link need an explanation that is not
   * "this flow has no integration". */
  integrationNotice?: JSX.Element | null;
  /** "Focus canvas" -- the header collapses to its first row (pills, title,
   * actions), giving the canvas below the rest of the viewport. Lifted to
   * `celigo-flow-page.tsx` (celigo flow sizing UI) rather than owned here:
   * the parent ALSO has to shrink the real header PANEL and hide the
   * divider between it and the canvas when this flips, neither of which
   * this component can reach on its own -- so it is the single source of
   * truth for both the boolean and its `localStorage` persistence, and this
   * component is purely presentational for it (same pattern the navigator
   * rail's `collapsed`/`onToggle` props already use for the SAME reason:
   * `celigo-flow-page.tsx`'s `navCollapsed`/`toggleNav`). */
  headerCollapsed: boolean;
  onToggleHeaderCollapsed: () => void;
}): JSX.Element {
  // The AI description is inherited free text (see the block below), not
  // authored for this UI -- some run to ten lines. Collapsed to a 2-line
  // excerpt by default (mockup: "a one-to-two-line excerpt with an
  // ellipsis"), a `useState` rather than derived from anything else since
  // it is purely a per-view reading preference, not a fact about the flow.
  const [aiExpanded, setAiExpanded] = useState(false);
  const aiDescriptionId = `celigo-ai-description-${detail.id}`;
  // A description that already fits in two lines gets no clamp and no
  // toggle -- a "Show more" that shows nothing more teaches the reader to
  // ignore the button. jsdom cannot measure line boxes, so "fits" is a
  // character budget: two header lines hold ~200 characters at the
  // narrowest inspector-open width.
  const aiText = detail.ai_description_detailed ?? detail.ai_description_summary;
  const aiClampable = !!aiText && aiText.length > AI_DESCRIPTION_CLAMP_CHARS;

  const syncSettled = syncStatusState === "success";
  const paused = detail.disabled === true;
  const parsed = parseSchedule(detail.schedule);
  const stall = stallState({
    schedule: detail.schedule,
    disabled: detail.disabled,
    lastExecutedAt: detail.last_executed_at,
    lastSyncedAt,
  });

  const { routerCount, branchCount } = computeTopologyCounts(detail);
  const lookupCount = detail.steps.filter((s) => s.kind === "lookup").length;
  const writes = computeFlowWrites(detail.steps);
  const scriptStats = computeScriptStats(detail);

  const lastRanBeforeSync =
    detail.last_executed_at && lastSyncedAt ? minutesBetween(detail.last_executed_at, lastSyncedAt) : null;

  function handleCopyLink() {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(window.location.href);
    }
  }

  const openInCeligoHref =
    integrationCeligoId && detail.celigo_id
      ? `https://integrator.io/integrations/${integrationCeligoId}/flowBuilder/${detail.celigo_id}`
      : null;

  return (
    <div className="flex flex-col gap-2 border-b bg-card px-4 py-2.5">
      <div className="flex flex-wrap items-center gap-2.5">
        {syncSettled ? (
          <>
            <ErrorPill
              count={detail.error_count}
              signatureCount={detail.signature_count}
              // The flow's OWN check timestamp, not the sync time -- a sync
              // can complete while this flow's errors were never re-checked
              // with the correct endpoint (see `ErrorPill`'s docstring).
              checkedAt={detail.errors_checked_at}
            />
            <SchedulePill stall={stall} parsed={parsed} />
          </>
        ) : (
          <SyncStatusPill state={syncStatusState} />
        )}
        <h3
          className="text-[18px] font-semibold tracking-tight"
          title={integrationName ? `${detail.name} — ${integrationName}` : detail.name}
        >
          {detail.name}
        </h3>
        <div className="ml-auto flex items-center gap-1.5">
          <button
            type="button"
            aria-pressed={headerCollapsed}
            onClick={onToggleHeaderCollapsed}
            className="rounded-md border px-2 py-1 text-[11.5px] text-muted-foreground hover:text-foreground"
          >
            {headerCollapsed ? "Show details" : "Focus canvas"}
          </button>
          <button
            type="button"
            onClick={handleCopyLink}
            className="rounded-md border px-2 py-1 text-[11.5px] text-muted-foreground hover:text-foreground"
          >
            Copy link
          </button>
          {openInCeligoHref && (
            <a
              href={openInCeligoHref}
              target="_blank"
              rel="noreferrer"
              className="rounded-md border px-2 py-1 text-[11.5px] text-muted-foreground hover:text-foreground"
            >
              Open in Celigo ↗
            </a>
          )}
        </div>
      </div>

      {syncStatusState === "error" && (
        <ErrorNotice message="Couldn't load sync status." onRetry={onRetrySyncStatus} />
      )}
      {integrationNotice}

      {/* "Focus canvas" collapses the facts below the first row -- pills,
          title and actions survive so the flow's own identity and status
          stay visible while the canvas gets the rest of the viewport
          (finding: the diagram was only getting half the height). The two
          error notices above are deliberately NOT collapsed: a failed
          request is something to act on, not a detail to tuck away. */}
      {!headerCollapsed && (
        <>
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[11.5px] tabular-nums text-muted-foreground">
          <span>
            {parsed.kind === "cron"
              ? `${paused ? "kept: " : ""}${parsed.label} ${parsed.display}${detail.timezone ? ` · ${detail.timezone}` : ""}`
              : parsed.kind === "on_demand"
                ? "on demand"
                : parsed.raw}
          </span>
          {detail.last_executed_at && lastSyncedAt && (
            <span>{`last ran ${formatUtcTime(detail.last_executed_at)} · ${Math.abs(lastRanBeforeSync ?? 0)} min ${
              (lastRanBeforeSync ?? 0) >= 0 ? "before" : "after"
            } the sync`}</span>
          )}
          <span>{`${detail.steps.length} step${detail.steps.length === 1 ? "" : "s"} · ${routerCount} router${routerCount === 1 ? "" : "s"} · ${branchCount} branch${branchCount === 1 ? "" : "es"} · ${lookupCount} lookup${lookupCount === 1 ? "" : "s"}`}</span>
          <span className="font-mono text-foreground">{formatFlowWritesLine(writes)}</span>
          <span className="flex items-center gap-1.5">
            <span>{`${scriptStats.count} script${scriptStats.count === 1 ? "" : "s"}`}</span>
            {scriptStats.divergedFamilies > 0 && (
              <Pill tone="warn">{`${scriptStats.divergedFamilies} diverged famil${scriptStats.divergedFamilies === 1 ? "y" : "ies"}`}</Pill>
            )}
          </span>
          {clonedFrom && (
            <span>
              {clonedFrom.resolvedName
                ? `cloned from ${clonedFrom.resolvedName}`
                : "cloned from a flow no longer in the account"}
            </span>
          )}
          <span>{`modified in Celigo ${formatFullDate(detail.celigo_last_modified)}`}</span>
        </div>

        <div data-testid="celigo-overview" className="flex flex-col gap-1.5">
          <div className="flex gap-2 text-[12.5px] text-foreground">
            <span className="shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground">
              What it does · derived
            </span>
            <span>{deriveFlowSummary(detail)}</span>
          </div>
          {aiText && (
            <div className="flex flex-col gap-1 rounded-md border border-border/70 bg-muted/40 px-2.5 py-1.5 text-[12px]">
              <div className="flex gap-2">
                <span className="shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground">
                  {`AI description · Celigo${detail.source_id ? " · inherited from the clone source" : ""}`}
                </span>
                {/* `line-clamp-2` alone sets `overflow: hidden` and the
                    `-webkit-box` display it needs -- no separate
                    `overflow-hidden` alongside it (see `step-bubble.tsx`'s
                    title, which notes `cn`/tailwind-merge would strip it back
                    out as a conflicting rule anyway). */}
                <q id={aiDescriptionId} className={cn("text-muted-foreground", aiClampable && !aiExpanded && "line-clamp-2")}>
                  {aiText}
                </q>
              </div>
              {aiClampable && (
                <button
                  type="button"
                  aria-expanded={aiExpanded}
                  aria-controls={aiDescriptionId}
                  onClick={() => setAiExpanded((prev) => !prev)}
                  className="self-start text-[10.5px] font-medium text-muted-foreground hover:text-foreground"
                >
                  {aiExpanded ? "Show less" : "Show more"}
                </button>
              )}
            </div>
          )}
          {/* "Synced —." was the same collapse the pills made: a dash standing
              in for three different truths. Each state says which one it is. */}
          <p className="text-[11px] text-muted-foreground">
            {syncSettled
              ? `Synced ${formatRelativeTime(lastSyncedAt)}.`
              : syncStatusState === "pending"
                ? "Checking sync status…"
                : "Sync status unavailable — every fact below is as of an unknown sync."}
          </p>
        </div>
        </>
      )}
    </div>
  );
}
