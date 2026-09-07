"use client";

/**
 * Task 5 — the Scripts view's detail pane (spec §3.3): header facts,
 * the versions strip, the source pane (with the N2 shield every surface
 * that shows customer JavaScript renders — `shared.tsx`'s `N2_SHIELD_TEXT`,
 * imported rather than re-typed, see that file's own docstring on why two
 * copies of a mandated string is the shape that drifts), a compare-any-two-
 * versions mode built on the workspace's existing `DiffViewer` (extended by
 * this task with `sideBySide`/`language`, see that file's docstring), and
 * the where-used table.
 *
 * This component owns exactly one prop, `dedupKey` — the caller
 * (`celigo-scripts-page.tsx`) only mounts it once a family is actually
 * selected, so there is no "no family" state to render here. Everything
 * else this pane needs — which member `copy=` named on arrival, whether
 * `compare=` is on the URL, and the two navigation actions
 * (`go.flow`/`go.scripts`) — comes from `useCeligoRoute()` directly rather
 * than threading through more props, matching how `go.flow`/`go.step` are
 * already called straight off the route hook elsewhere in this surface
 * (`celigo-flow-page.tsx`) rather than passed down as callbacks.
 *
 * `go.flow(id, integrationId)` is the ONLY navigation the where-used row's
 * "↗" (and the header's "Open in flow map") use — its shipped signature
 * (Task 3) takes no step/site payload, so this pane can name the target
 * FLOW precisely but not pre-select a step/script on arrival; landing on
 * the flow itself (rather than nothing) is the honest scope of what that
 * helper can do today.
 */

import { useEffect, useState } from "react";
import { Copy as CopyIcon } from "lucide-react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import {
  useCeligoScriptFamily,
  type CeligoScriptFamilyMember,
  type CeligoScriptFamilySite,
  type CeligoScriptFamilyVersion,
} from "@/hooks/use-celigo-flows";
import { queryState } from "@/lib/query-state";
import { cn } from "@/lib/utils";
import { DiffViewer } from "@/components/workspace/diff-viewer";
import { ErrorNotice, N2_SHIELD_TEXT, Pill } from "../shared";
import { useCeligoRoute, type ScriptsCompare } from "../celigo-route";

const KIND_CODE: Record<string, string> = {
  hook: "HK",
  transform: "TR",
  filter: "FL",
  router: "RT",
  mixed: "MX",
  unattached: "—",
};

const ROWS_SHOWN_DEFAULT = 8;

// ---------------------------------------------------------------------------
// Pure helpers — exported and unit-tested on their own (spec §3.3's
// "Produces" list).
// ---------------------------------------------------------------------------

/** The Compare control's default pair (spec §3.3): the OLDEST content
 * against whichever version currently holds the original, or the newest
 * version when no member in production is the original. `versions` arrives
 * already ordered by letter (`ScriptFamilyDetail.versions`), and letters are
 * assigned by first-appearance order (spec §2.2) — so `versions[0]` IS the
 * oldest content and `versions[versions.length - 1]` IS the newest, with no
 * separate sort needed here.
 *
 * The one edge case worth a comment: if the original's OWN version happens
 * to be the oldest one, "oldest -> the original's version" would pair a
 * version against itself — a no-op diff. Falling back to oldest -> newest
 * in that case is a REASONABLE default, not a spec-mandated one (the spec
 * text does not name this edge), so please read this as good judgement, not
 * a stated rule if that surprises a future reader. */
export function defaultComparePair(versions: CeligoScriptFamilyVersion[]): ScriptsCompare | null {
  if (versions.length < 2) return null;
  const oldest = versions[0];
  const newest = versions[versions.length - 1];
  const withOriginal = versions.find((v) => v.holds_original);
  const target = withOriginal && withOriginal.letter !== oldest.letter ? withOriginal : newest;
  return { left: oldest.letter, right: target.letter };
}

/** Resolves the drawer's hand-off (`?copy=<script_id>`) to that member's
 * version letter, so arriving from the drawer selects the exact version the
 * reader was already looking at rather than this pane's own default. `null`
 * for a missing/unrecognised id (an old link, a member Never carried a
 * version because it has no content hash) — the caller falls through to its
 * own default in that case, never throws. */
export function versionForCopy(members: CeligoScriptFamilyMember[], copyId: string | null): string | null {
  if (!copyId) return null;
  const found = members.find((m) => m.script_id === copyId);
  return found?.version_letter ?? null;
}

// ---------------------------------------------------------------------------
// Small formatters — kept local; each mirrors an established convention
// elsewhere in this surface (see the comment at each call site) rather than
// inventing a new one.
// ---------------------------------------------------------------------------

/** `(bytes/1024).toFixed(1)} KB` — same convention as
 * `settings/celigo-script-viewer.tsx`'s `formatContentSize`. `null` (no
 * size recorded) omits the fact rather than printing a fabricated "0.0 KB". */
function formatSize(bytes: number | null): string | null {
  if (bytes === null) return null;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

const MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "23 Apr 2026" — UTC, not local time (same discipline as
 * `celigo-scripts-page.tsx`'s `ScriptsSyncPill`, which reads
 * `getUTCHours`/`getUTCMinutes` rather than the browser's zone), so this
 * renders identically in a snapshot and in CI regardless of the machine's
 * timezone. `null`/unparseable -> "—", never a fabricated date. */
function formatShortDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.getUTCDate()} ${MONTH_ABBR[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

/** The member holding a given version letter's content — versions
 * themselves (`ScriptFamilyVersion`) carry no `content` field (spec §2.2:
 * that lives on `ScriptFamilyMember`), so showing or diffing a version's
 * body means finding any one member wired to that letter. `""` when none is
 * found (should not happen for a letter this pane itself offers) rather
 * than `undefined`, so a diff viewer never receives `undefined` as text. */
function contentForVersion(members: CeligoScriptFamilyMember[], letter: string | null): string {
  if (!letter) return "";
  return members.find((m) => m.version_letter === letter)?.content ?? "";
}

/** `role`/`adaptor_type` -> the same Source/Lookup/Destination/Router
 * vocabulary `settings/celigo-script-viewer.tsx`'s `siteLocationLabel`
 * already uses for the identical fact on the drawer — an `*Export` adaptor
 * on a processor is Celigo's own marker for a lookup. */
function stepKindWord(role: string | null, adaptorType: string | null): string {
  if (!role) return "Router";
  if (role === "generator") return "Source";
  return adaptorType?.endsWith("Export") ? "Lookup" : "Destination";
}

/** "C · original" / "B · clone 24 Mar 2026" (spec §3.3's Copy column) — the
 * SITE only carries `version_letter`/`script_id`; whether that copy IS the
 * original, and when it was last modified, lives on the matching MEMBER
 * (`is_original`, `celigo_last_modified`). "—" when the site has no
 * resolvable script (never synced) rather than a fabricated letter. */
function copyLabel(site: CeligoScriptFamilySite, members: CeligoScriptFamilyMember[]): string {
  if (!site.version_letter) return "—";
  const member = members.find((m) => m.script_id === site.script_id);
  if (member?.is_original) return `${site.version_letter} · original`;
  return `${site.version_letter} · clone ${formatShortDate(member?.celigo_last_modified ?? null)}`;
}

// ---------------------------------------------------------------------------
// Where-used row pieces
// ---------------------------------------------------------------------------

/** The Errors cell's four states (spec §3.3): a router-level site has no
 * owning step, so it has no error count to report at all (`—`, mute) — not
 * "0", which would claim a check that cannot have happened for something
 * with no step. Otherwise this is the exact "a zero without a timestamp is
 * not a claim" rule `shared.tsx`'s `ErrorPill` already states for flows,
 * applied to one where-used row instead of a whole flow. */
function ErrorsCell({ site }: { site: CeligoScriptFamilySite }): JSX.Element {
  if (site.flow_step_id === null) {
    return (
      <Pill tone="mute" dot="hollow">
        —
      </Pill>
    );
  }
  if (site.errors_checked_at === null) {
    return (
      <Pill tone="mute" dot="hollow">
        not checked
      </Pill>
    );
  }
  if ((site.open_error_count ?? 0) === 0) {
    return (
      <Pill tone="ok" dot="solid">
        0
      </Pill>
    );
  }
  return (
    <Pill tone="crit" dot="solid">
      {site.open_error_count} open
    </Pill>
  );
}

function SiteRow({
  site,
  members,
  onOpen,
}: {
  site: CeligoScriptFamilySite;
  members: CeligoScriptFamilyMember[];
  onOpen: (site: CeligoScriptFamilySite) => void;
}): JSX.Element {
  const kindWord = stepKindWord(site.step_role, site.step_adaptor_type);
  const stepMain = site.step_reference_name ?? kindWord;
  return (
    <tr className="border-b last:border-b-0">
      <td className="px-2 py-1.5 align-top text-[12.5px]">{site.integration_name ?? "—"}</td>
      <td className="px-2 py-1.5 align-top text-[12.5px]">
        <p>{site.flow_name}</p>
        {site.flow_disabled && (
          <Pill tone="mute" dot="hollow">
            paused
          </Pill>
        )}
      </td>
      <td className="px-2 py-1.5 align-top text-[12.5px]">
        <p>{stepMain}</p>
        {site.flow_step_id !== null && (
          <p className="text-[11px] text-muted-foreground">
            {kindWord} · {site.step_adaptor_type ?? "adaptor not synced"}
          </p>
        )}
      </td>
      <td className="whitespace-nowrap px-2 py-1.5 align-top font-mono text-[12px]">{site.json_path}</td>
      <td className="whitespace-nowrap px-2 py-1.5 align-top text-[12px] tabular-nums">
        {copyLabel(site, members)}
      </td>
      <td className="px-2 py-1.5 align-top">
        <ErrorsCell site={site} />
      </td>
      <td className="px-2 py-1.5 align-top">
        <button
          type="button"
          aria-label={`Open ${site.flow_name} in flow map`}
          className="text-muted-foreground hover:text-foreground"
          onClick={() => onOpen(site)}
        >
          ↗
        </button>
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Versions strip
// ---------------------------------------------------------------------------

function VersionCard({
  version,
  selected,
  onSelect,
}: {
  version: CeligoScriptFamilyVersion;
  selected: boolean;
  onSelect: (letter: string) => void;
}): JSX.Element {
  const mark = version.holds_original ? "✓ original" : version.sites_count === 0 ? "spare copy" : null;
  return (
    <button
      type="button"
      aria-pressed={selected}
      aria-label={`Version ${version.letter}`}
      onClick={() => onSelect(version.letter)}
      className={cn(
        "flex min-w-[160px] flex-col gap-0.5 rounded-lg border px-2.5 py-1.5 text-left text-[11.5px]",
        selected && "border-accent bg-accent/10",
      )}
    >
      <span className="flex items-center gap-1.5">
        <span className="inline-flex h-5 w-5 items-center justify-center rounded border font-bold">
          {version.letter}
        </span>
        <b>
          {version.copies_count} cop{version.copies_count === 1 ? "y" : "ies"}
        </b>
        <span className="text-muted-foreground">
          · {version.sites_count} site{version.sites_count === 1 ? "" : "s"}
        </span>
      </span>
      <span className="text-muted-foreground">
        first seen {formatShortDate(version.first_seen)}
        {formatSize(version.size_bytes) ? ` · ${formatSize(version.size_bytes)}` : ""}
      </span>
      {mark && (
        <span className={cn(version.holds_original ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground")}>
          {mark}
        </span>
      )}
    </button>
  );
}

// ---------------------------------------------------------------------------
// The pane
// ---------------------------------------------------------------------------

export function CeligoScriptsDetail({ dedupKey }: { dedupKey: string }): JSX.Element {
  const route = useCeligoRoute();
  const query = useCeligoScriptFamily(dedupKey);
  const state = queryState(query);

  // The version shown in (non-compare) source mode. Seeded from `copy=`'s
  // hand-off when present, else the version holding the original, else the
  // oldest — see `versionForCopy`/`defaultComparePair`'s docstrings for why
  // those are the right defaults. Re-derived whenever the family or the
  // `copy=` param changes (a fresh drawer hand-off, or navigating to a
  // different family entirely) rather than staying stuck on a stale letter
  // from the PREVIOUS family's data.
  const [selectedLetter, setSelectedLetter] = useState<string | null>(null);
  const [sideBySide, setSideBySide] = useState(true);
  const [showAllSites, setShowAllSites] = useState(false);

  const data = state === "success" ? query.data! : null;

  useEffect(() => {
    if (!data) return;
    const fromCopy = versionForCopy(data.members, route.copyId);
    if (fromCopy) {
      setSelectedLetter(fromCopy);
      return;
    }
    const withOriginal = data.versions.find((v) => v.holds_original);
    setSelectedLetter(withOriginal?.letter ?? data.versions[0]?.letter ?? null);
    // Only re-derive on a genuinely NEW family/hand-off — not on every
    // render, which would fight a manual version-card click below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dedupKey, route.copyId, data?.summary.dedup_key]);

  useEffect(() => {
    setShowAllSites(false);
  }, [dedupKey]);

  if (state === "pending") {
    return (
      <div className="flex h-full items-center justify-center p-6 text-[13px] text-muted-foreground">
        <span className="sr-only">Loading family…</span>
        <div aria-hidden className="h-24 w-full max-w-sm animate-pulse rounded-lg bg-muted" />
      </div>
    );
  }
  if (state === "error") {
    return (
      <div className="p-4">
        <ErrorNotice message="Couldn't load this family." onRetry={() => query.refetch()} />
      </div>
    );
  }

  const detail = query.data!;
  const { summary, members, versions, sites } = detail;

  // Every `go.scripts` call below re-states the current scripts param set
  // before overriding the field it means to change — `go.scripts` REPLACES
  // the whole param set rather than merging (spec §3.1), same discipline
  // `celigo-scripts-page.tsx`'s own `scriptsBase` follows for the same
  // reason: a compare-pair change must not silently drop the current
  // filter/kind/q/integration selection.
  function scriptsBase() {
    return {
      family: dedupKey,
      in: route.scriptsIntegrationId,
      filter: route.scriptsFilter,
      kind: route.scriptsKind,
      q: route.q,
    };
  }

  const compare = route.compare;
  const isComparing = !!compare;
  const canCompare = versions.length >= 2;
  const hasSites = sites.length > 0;

  function onToggleCompare() {
    if (isComparing) {
      route.go.scripts({ ...scriptsBase(), compare: null });
      return;
    }
    const pair = defaultComparePair(versions);
    if (pair) route.go.scripts({ ...scriptsBase(), compare: pair });
  }

  function onChangeCompareSide(side: "left" | "right", letter: string) {
    if (!compare) return;
    const next = side === "left" ? { left: letter, right: compare.right } : { left: compare.left, right: letter };
    route.go.scripts({ ...scriptsBase(), compare: next });
  }

  function onOpenSite(site: CeligoScriptFamilySite) {
    route.go.flow(site.flow_id, site.integration_id ?? undefined);
  }

  function onOpenInFlowMap() {
    const first = sites[0];
    if (!first) return;
    route.go.flow(first.flow_id, first.integration_id ?? undefined);
  }

  function onCopySource() {
    const letter = isComparing ? compare!.right : selectedLetter;
    const content = contentForVersion(members, letter);
    navigator.clipboard?.writeText(content);
  }

  const shownSites = showAllSites ? sites : sites.slice(0, ROWS_SHOWN_DEFAULT);
  const sizeLabel = formatSize(summary.max_size_bytes);
  const modifiedLabel = summary.last_modified ? `modified ${formatShortDate(summary.last_modified)}` : null;

  const selectedVersion = versions.find((v) => v.letter === selectedLetter) ?? null;
  let sourceBarText = `Showing version ${selectedLetter ?? "—"}`;
  if (summary.original_present && selectedVersion) {
    sourceBarText += selectedVersion.holds_original ? " · the original copy" : " · a clone";
  }

  return (
    <div data-testid="celigo-scripts-detail" className="flex h-full min-h-0 flex-col gap-3 overflow-auto p-3">
      {/* Header */}
      <div data-testid="scripts-detail-header" className="flex flex-wrap items-center gap-2 border-b pb-2">
        <span className="inline-flex h-5 w-6 items-center justify-center rounded border text-[10px] font-bold">
          {KIND_CODE[summary.kind] ?? "—"}
        </span>
        <h3 className="font-mono text-[14px] font-semibold">{summary.name}</h3>
        <span className="text-muted-foreground">·</span>
        <span className="tabular-nums">
          {summary.copies_count} cop{summary.copies_count === 1 ? "y" : "ies"}
        </span>
        <span className="text-muted-foreground">·</span>
        <span className={cn("tabular-nums", summary.content_diverged && "text-amber-600 dark:text-amber-400")}>
          {summary.versions_count} version{summary.versions_count === 1 ? "" : "s"}
        </span>
        {summary.function_name && (
          <>
            <span className="text-muted-foreground">·</span>
            <span>{summary.function_name}</span>
          </>
        )}
        {sizeLabel && (
          <>
            <span className="text-muted-foreground">·</span>
            <span className="tabular-nums">{sizeLabel}</span>
          </>
        )}
        {modifiedLabel && (
          <>
            <span className="text-muted-foreground">·</span>
            <span>{modifiedLabel}</span>
          </>
        )}
        <span className="grow" />
        <button
          type="button"
          aria-pressed={isComparing}
          disabled={!canCompare}
          onClick={onToggleCompare}
          className={cn("rounded-md border px-2 py-1 text-[12px]", isComparing && "border-accent bg-accent/10")}
        >
          Compare versions ⇄
        </button>
        <button
          type="button"
          disabled={!hasSites}
          onClick={onOpenInFlowMap}
          className="rounded-md border px-2 py-1 text-[12px]"
        >
          Open in flow map ↗
        </button>
        <button type="button" onClick={onCopySource} className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[12px]">
          <CopyIcon className="h-3 w-3" aria-hidden />
          Copy source
        </button>
      </div>

      {/* Versions strip */}
      <div className="flex flex-wrap gap-2" data-testid="scripts-versions-strip">
        {versions.map((v) => (
          <VersionCard
            key={v.letter}
            version={v}
            selected={isComparing ? v.letter === compare!.left || v.letter === compare!.right : v.letter === selectedLetter}
            onSelect={(letter) => setSelectedLetter(letter)}
          />
        ))}
      </div>

      {isComparing ? (
        <>
          <div className="flex flex-wrap items-center gap-2 border-b pb-2 text-[12px]">
            <span>
              Comparing <b>{compare!.left}</b> ({formatShortDate(versions.find((v) => v.letter === compare!.left)?.first_seen ?? null)})
              {" → "}
              <b>{compare!.right}</b>
              {versions.find((v) => v.letter === compare!.right)?.holds_original ? " original" : ""} (
              {formatShortDate(versions.find((v) => v.letter === compare!.right)?.first_seen ?? null)})
            </span>
            <label className="flex items-center gap-1">
              Left
              <select
                aria-label="Compare left version"
                value={compare!.left}
                onChange={(e) => onChangeCompareSide("left", e.target.value)}
                className="rounded-md border bg-card px-1 py-0.5"
              >
                {versions.map((v) => (
                  <option key={v.letter} value={v.letter}>
                    {v.letter}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1">
              Right
              <select
                aria-label="Compare right version"
                value={compare!.right}
                onChange={(e) => onChangeCompareSide("right", e.target.value)}
                className="rounded-md border bg-card px-1 py-0.5"
              >
                {versions.map((v) => (
                  <option key={v.letter} value={v.letter}>
                    {v.letter}
                  </option>
                ))}
              </select>
            </label>
            <span className="grow" />
            <button
              type="button"
              aria-pressed={sideBySide}
              onClick={() => setSideBySide(true)}
              className={cn("rounded-md border px-2 py-0.5", sideBySide && "border-accent bg-accent/10")}
            >
              Side by side
            </button>
            <button
              type="button"
              aria-pressed={!sideBySide}
              onClick={() => setSideBySide(false)}
              className={cn("rounded-md border px-2 py-0.5", !sideBySide && "border-accent bg-accent/10")}
            >
              Inline
            </button>
          </div>
          <div className="min-h-[240px] flex-1 rounded-lg border">
            <DiffViewer
              original={contentForVersion(members, compare!.left)}
              modified={contentForVersion(members, compare!.right)}
              filePath={summary.name}
              language="javascript"
              sideBySide={sideBySide}
            />
          </div>
        </>
      ) : (
        <>
          <div className="border-b pb-1.5 text-[12px] text-muted-foreground">{sourceBarText}</div>
          <div className="max-h-[320px] overflow-auto rounded-lg border">
            <SyntaxHighlighter
              language="javascript"
              style={oneDark}
              showLineNumbers
              wrapLongLines
              customStyle={{ margin: 0, padding: "1rem", fontSize: "12px" }}
            >
              {contentForVersion(members, selectedLetter) || "// No source recorded for this version."}
            </SyntaxHighlighter>
          </div>
        </>
      )}

      <div className="flex items-start gap-2.5 rounded-xl border border-amber-500/30 bg-amber-500/5 p-3">
        <p className="text-[12px] text-muted-foreground">{N2_SHIELD_TEXT}</p>
      </div>

      {/* Where used */}
      <div className="border-b pb-1 text-[12.5px] font-medium">
        Where used <span className="text-muted-foreground">· {sites.length} site{sites.length === 1 ? "" : "s"}</span>
      </div>
      {sites.length === 0 ? (
        <p className="text-[12.5px] text-muted-foreground">
          No production flow references any of these {summary.copies_count} copies. None of the production flows
          we sync names this script. That does not mean unused: sandbox flows are not synced, and Celigo can
          reference a script from places this map does not walk.
        </p>
      ) : (
        <>
          <div className="overflow-x-auto rounded-lg border">
            <table className="w-full text-left">
              <thead>
                <tr className="border-b bg-muted/40 text-[11px] uppercase tracking-wide text-muted-foreground">
                  <th className="px-2 py-1.5 font-medium">Integration</th>
                  <th className="px-2 py-1.5 font-medium">Flow</th>
                  <th className="px-2 py-1.5 font-medium">Step</th>
                  <th className="px-2 py-1.5 font-medium">Where</th>
                  <th className="px-2 py-1.5 font-medium">Copy</th>
                  <th className="px-2 py-1.5 font-medium">Errors</th>
                  <th className="px-2 py-1.5 font-medium" />
                </tr>
              </thead>
              <tbody>
                {shownSites.map((s) => (
                  <SiteRow key={s.attachment_id} site={s} members={members} onOpen={onOpenSite} />
                ))}
              </tbody>
            </table>
          </div>
          {sites.length > ROWS_SHOWN_DEFAULT && (
            <div className="text-[11.5px] text-muted-foreground">
              {showAllSites ? sites.length : Math.min(ROWS_SHOWN_DEFAULT, sites.length)} of {sites.length} shown ·{" "}
              {!showAllSites && (
                <button type="button" className="font-medium text-foreground underline" onClick={() => setShowAllSites(true)}>
                  Show all {sites.length}
                </button>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
