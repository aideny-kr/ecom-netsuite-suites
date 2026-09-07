"use client";

/**
 * Task 9 — the Celigo surface's URL state, and its single writer.
 *
 * The surface is deliberately URL-driven rather than component `useState`:
 * a deep link (a saved bookmark, a Slack link to a specific step) has to
 * reproduce exactly where a viewer was, and refreshing the tab must not
 * lose the drill-down. `readCeligoRoute` is the one place that decodes the
 * query string; `useCeligoRoute().go` is the one place that encodes it back
 * — every navigation inside the Celigo surface goes through a `go.*` call
 * instead of a raw `router.push`, so "what survives a navigation, what gets
 * cleared, which params take precedence" is a single decision instead of
 * one made ad hoc at each call site.
 */

import { useCallback, useMemo } from "react";
import { useRouter, useSearchParams, usePathname } from "next/navigation";

export type CeligoView = "tiles" | "list";
export type CeligoTab = "flows" | "scripts" | "errors" | "changes";

/** The Scripts view's list filter chips (spec §3.3). `"all"` is the default
 * and is never written to the URL (see `go.scripts`). */
export type ScriptsFilter = "all" | "attached" | "unattached" | "diverged" | "errors";

/** The Scripts view's kind filter (spec §3.1's `kind` param) -- deliberately
 * NOT the same set as `ScriptFamilySummary.kind` (which also has `"mixed"`,
 * a family whose sites disagree, never a filter a reader picks). `null` =
 * every kind, and is never written to the URL. */
export type ScriptsKind = "hook" | "transform" | "filter" | "router" | "unattached";

/** Two version letters in compare mode (`?compare=A..D`), oldest-first by
 * convention at the call site — this type itself does not order them. */
export type ScriptsCompare = { left: string; right: string };

export type CeligoRoute = {
  surface: "files" | "celigo";
  view: CeligoView;
  integrationId: string | null;
  tab: CeligoTab;
  flowId: string | null;
  stepId: string | null;
  scriptId: string | null;
  /** WHICH attachment site of `scriptId` is open, as the attachment's own
   * `json_path`. One script is routinely wired in at several sites on the
   * same step (a preMap and a postMap, or two clones of one family), and the
   * step id alone cannot tell them apart — so the drawer named whichever site
   * the backend returned first. On the URL, so a pasted link reopens the same
   * one. `null` when no site was named (an older link, or a caller that has
   * only the script). */
  scriptSite: string | null;
  // ---- Scripts view (spec §3.1) — all optional, all read via `?tab=scripts`
  // with no `integration` on the URL (see `isScriptsView`). ----
  /** Selected family's `dedup_key` (`?family=`). */
  familyKey: string | null;
  /** A member `script_id` (`?copy=`) — selects that member's version on
   * arrival, the drawer's hand-off into the Scripts view. */
  copyId: string | null;
  /** `?compare=A..D` — presence means compare mode. Malformed (missing the
   * `..` separator, or either side empty) normalises to `null` rather than
   * throwing. */
  compare: ScriptsCompare | null;
  scriptsFilter: ScriptsFilter;
  /** `null` = every kind (no `?kind=`, or an unrecognised one). */
  scriptsKind: ScriptsKind | null;
  /** The integration FILTER (`?in=`) — deliberately a different key from
   * `integration` (spec §3.1): `integration=X&tab=scripts` already addresses
   * the integration page's own Scripts tab and must keep doing so. */
  scriptsIntegrationId: string | null;
  /** Client-side search text (`?q=`). Always a string, `""` when absent. */
  q: string;
};

const VALID_VIEWS: readonly CeligoView[] = ["tiles", "list"];
const VALID_TABS: readonly CeligoTab[] = ["flows", "scripts", "errors", "changes"];
const VALID_SCRIPTS_FILTERS: readonly ScriptsFilter[] = [
  "all",
  "attached",
  "unattached",
  "diverged",
  "errors",
];
const VALID_SCRIPTS_KINDS: readonly ScriptsKind[] = [
  "hook",
  "transform",
  "filter",
  "router",
  "unattached",
];

/** Parses `?compare=A..D` into `{ left: "A", right: "D" }`. Anything that
 * isn't exactly two non-empty parts joined by `".."` (no separator, an empty
 * side, an empty param) reads as "not comparing" rather than throwing —
 * same normalise-don't-throw rule as `view`/`tab` above. */
function parseCompare(raw: string | null): ScriptsCompare | null {
  if (!raw) return null;
  const sep = raw.indexOf("..");
  if (sep < 1) return null; // no separator, or an empty left side
  const left = raw.slice(0, sep);
  const right = raw.slice(sep + 2);
  if (!left || !right) return null;
  return { left, right };
}

/** Serialises a `ScriptsCompare` back to its `A..D` URL form. */
function formatCompare(compare: ScriptsCompare): string {
  return `${compare.left}..${compare.right}`;
}

/**
 * Pure read of the Celigo slice of the URL — no router, no side effects, so
 * it can be unit-tested with a bare `URLSearchParams` and reused anywhere a
 * route needs decoding without mounting a component. An unrecognised
 * `view`/`tab` value (a stale bookmark from a since-renamed enum, a hand-
 * edited URL) normalises to its default rather than propagating a typo
 * into app state that every consumer would otherwise have to guard against.
 */
export function readCeligoRoute(params: URLSearchParams): CeligoRoute {
  const viewParam = params.get("view");
  const tabParam = params.get("tab");
  const filterParam = params.get("filter");
  const kindParam = params.get("kind");
  return {
    surface: params.get("surface") === "celigo" ? "celigo" : "files",
    view: VALID_VIEWS.includes(viewParam as CeligoView) ? (viewParam as CeligoView) : "tiles",
    integrationId: params.get("integration"),
    tab: VALID_TABS.includes(tabParam as CeligoTab) ? (tabParam as CeligoTab) : "flows",
    flowId: params.get("flow"),
    stepId: params.get("step"),
    scriptId: params.get("script"),
    scriptSite: params.get("site"),
    familyKey: params.get("family"),
    copyId: params.get("copy"),
    compare: parseCompare(params.get("compare")),
    scriptsFilter: VALID_SCRIPTS_FILTERS.includes(filterParam as ScriptsFilter)
      ? (filterParam as ScriptsFilter)
      : "all",
    scriptsKind: VALID_SCRIPTS_KINDS.includes(kindParam as ScriptsKind) ? (kindParam as ScriptsKind) : null,
    scriptsIntegrationId: params.get("in"),
    q: params.get("q") ?? "",
  };
}

/** `tab === "scripts"` alone is ambiguous: the integration page reuses the
 * same tab value for ITS OWN Scripts tab (`?integration=X&tab=scripts`,
 * spec §3.1). The account-wide Scripts view is that tab with no
 * `integration` and no `flow` on the URL — this is the one place that
 * distinguishes the two, so `celigo-surface.tsx` and every entry point below
 * ask this rather than re-deriving the condition. */
export function isScriptsView(
  route: Pick<CeligoRoute, "tab" | "integrationId" | "flowId">,
): boolean {
  return route.tab === "scripts" && !route.integrationId && !route.flowId;
}

// Fixed serialization order, independent of the order params happened to
// arrive in — so two calls that set the same fields always produce byte-
// identical URLs (stable history entries, stable test assertions).
const CELIGO_KEYS = [
  "surface",
  "view",
  "integration",
  "tab",
  "flow",
  "step",
  "script",
  "site",
  "family",
  "copy",
  "compare",
  "filter",
  "kind",
  "q",
  "in",
] as const;
type CeligoKey = (typeof CELIGO_KEYS)[number];
const CELIGO_KEY_SET: ReadonlySet<string> = new Set(CELIGO_KEYS);

/** Every current param that is neither one of the seven Celigo keys nor in
 * `drop` — i.e. whatever the rest of the app (today: `file`/`workspace`)
 * put on the URL, preserved verbatim and in its original order. */
function otherParams(current: URLSearchParams, drop: readonly string[]): Array<[string, string]> {
  const out: Array<[string, string]> = [];
  current.forEach((value, key) => {
    if (CELIGO_KEY_SET.has(key)) return;
    if (drop.includes(key)) return;
    out.push([key, value]);
  });
  return out;
}

function buildUrl(
  pathname: string,
  other: Array<[string, string]>,
  celigo: Partial<Record<CeligoKey, string>>,
): string {
  const params = new URLSearchParams();
  for (const [key, value] of other) params.append(key, value);
  for (const key of CELIGO_KEYS) {
    const value = celigo[key];
    if (value !== undefined) params.append(key, value);
  }
  const qs = params.toString();
  return qs ? `${pathname}?${qs}` : pathname;
}

/**
 * The current Celigo route, plus `go` — the only writer. `go.files()` and
 * `go.integrations()`/`go.integration()`/`go.flow()` push a new history
 * entry (they land on a different page); `go.step()`/`go.script()` replace
 * (a selection within the page already on screen, not a new page — a user
 * should not have to press Back once per bubble they clicked).
 */
export function useCeligoRoute(): CeligoRoute & {
  go: {
    files(): void;
    integrations(view?: CeligoView): void;
    integration(id: string, tab?: CeligoTab): void;
    /** Switch the integration page's tab, or the integrations page's
     * tiles/list view, WITHOUT leaving the page. Both replace (like
     * `step`/`script`) rather than push: a tab and a view toggle are
     * selections inside the page already on screen, and pushing made Back
     * walk one entry per tab a reader had glanced at instead of returning
     * them to where they came from. Everything else on the URL is kept. */
    tab(tab: CeligoTab): void;
    view(view: CeligoView): void;
    /** `integrationId` is the integration THIS flow belongs to, and wins over
     * whatever the current page carries. Every caller that knows it must pass
     * it: the ⌘K palette searches across all integrations, so defaulting to
     * the current one opened a flow under an integration that does not
     * contain it (wrong breadcrumb, wrong sibling list). Omit it only where
     * the flow is known to belong to the page already on screen. */
    /** `site`, added for the Scripts view's where-used "↗" (spec §3.3),
     * carries the flow's own step/script/site triple through to the new
     * page in one push — the same fields `go.step`/`go.script` write for a
     * same-page selection, so a caller that already knows exactly which
     * attachment it wants (not just which flow) can land on it directly
     * instead of only naming the flow. `jsonPath` is dropped without a
     * `scriptId`, same rule as `go.script`: a site only means anything
     * alongside the script it belongs to. */
    flow(
      id: string,
      integrationId?: string | null,
      site?: { stepId?: string | null; scriptId?: string | null; jsonPath?: string | null },
    ): void;
    step(stepId: string | null): void;
    /** `site` is the clicked attachment's own `json_path` — WHICH of a
     * script's several attachment sites is open (see `CeligoRoute.scriptSite`).
     * `stepId` overrides the step already on the URL for the rare caller that
     * knows better; every caller today omits it. Both are dropped when
     * `scriptId` is null: closing the drawer clears the whole selection. */
    script(scriptId: string | null, site?: { stepId?: string | null; jsonPath?: string | null }): void;
    /** The ONLY way into the account-wide Scripts view (spec §3.1/§3.2) —
     * pushes (a page-level destination, the same category as
     * `go.integration`/`go.flow`, not a same-page selection), and NEVER
     * writes `integration`: an integration filter travels as `in=` instead,
     * because `integration=X&tab=scripts` already means the integration
     * page's OWN Scripts tab (`go.integration(id, "scripts")`) and must keep
     * meaning that. Every field is the WHOLE next state, not a merge with
     * whatever the URL currently carries — a caller that wants to keep the
     * current filter while selecting a different family re-passes it (the
     * same discipline `go.flow`/`go.integration` already use: nothing
     * carries forward that the caller didn't ask for). Also drops
     * `flow`/`step`/`script`/`site`/`view` — the Scripts view owns none of
     * those. */
    scripts(opts?: {
      family?: string | null;
      copy?: string | null;
      in?: string | null;
      filter?: ScriptsFilter;
      kind?: ScriptsKind | null;
      q?: string;
      compare?: ScriptsCompare | null;
    }): void;
  };
} {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const route = useMemo(() => readCeligoRoute(searchParams), [searchParams]);

  const files = useCallback(() => {
    const other = otherParams(searchParams, []);
    router.push(buildUrl(pathname, other, {}));
  }, [searchParams, pathname, router]);

  const integrations = useCallback(
    (view: CeligoView = "tiles") => {
      const other = otherParams(searchParams, ["file", "workspace"]);
      router.push(
        buildUrl(pathname, other, {
          surface: "celigo",
          ...(view === "list" ? { view: "list" as const } : {}),
        }),
      );
    },
    [searchParams, pathname, router],
  );

  const integration = useCallback(
    (id: string, tab: CeligoTab = "flows") => {
      const other = otherParams(searchParams, ["file", "workspace"]);
      router.push(
        buildUrl(pathname, other, {
          surface: "celigo",
          integration: id,
          ...(tab !== "flows" ? { tab } : {}),
        }),
      );
    },
    [searchParams, pathname, router],
  );

  // `go.tab` / `go.view` — same-page selection changes, so they `replace` and
  // carry every other Celigo param through untouched. Both are written as one
  // shared builder so "which params survive a selection change" stays a
  // single decision rather than two that can drift.
  const replaceSelection = useCallback(
    (next: { view?: CeligoView; tab?: CeligoTab }) => {
      const view = next.view ?? route.view;
      const tab = next.tab ?? route.tab;
      const other = otherParams(searchParams, []);
      router.replace(
        buildUrl(pathname, other, {
          surface: "celigo",
          ...(view === "list" ? { view: "list" as const } : {}),
          ...(route.integrationId ? { integration: route.integrationId } : {}),
          ...(tab !== "flows" ? { tab } : {}),
          ...(route.flowId ? { flow: route.flowId } : {}),
          ...(route.stepId ? { step: route.stepId } : {}),
          ...(route.scriptId ? { script: route.scriptId } : {}),
          ...(route.scriptId && route.scriptSite ? { site: route.scriptSite } : {}),
        }),
      );
    },
    [
      searchParams,
      pathname,
      router,
      route.view,
      route.tab,
      route.integrationId,
      route.flowId,
      route.stepId,
      route.scriptId,
      route.scriptSite,
    ],
  );

  const tab = useCallback((next: CeligoTab) => replaceSelection({ tab: next }), [replaceSelection]);
  const view = useCallback((next: CeligoView) => replaceSelection({ view: next }), [replaceSelection]);

  const flow = useCallback(
    (
      id: string,
      integrationId?: string | null,
      site?: { stepId?: string | null; scriptId?: string | null; jsonPath?: string | null },
    ) => {
      const other = otherParams(searchParams, ["file", "workspace"]);
      // An explicitly-passed integration wins over the current page's: the
      // caller knows which integration owns THIS flow, and the page on
      // screen need not be it (the ⌘K palette lists every integration's
      // flows). `undefined` means "not stated", so fall back to the current
      // one; a caller that means "no integration" passes `null`.
      const owner = integrationId !== undefined ? integrationId : route.integrationId;
      router.push(
        buildUrl(pathname, other, {
          surface: "celigo",
          ...(owner ? { integration: owner } : {}),
          flow: id,
          ...(site?.stepId ? { step: site.stepId } : {}),
          ...(site?.scriptId ? { script: site.scriptId } : {}),
          ...(site?.scriptId && site?.jsonPath ? { site: site.jsonPath } : {}),
        }),
      );
    },
    [searchParams, pathname, router, route.integrationId],
  );

  const step = useCallback(
    (stepId: string | null) => {
      const other = otherParams(searchParams, []);
      router.replace(
        buildUrl(pathname, other, {
          surface: "celigo",
          ...(route.integrationId ? { integration: route.integrationId } : {}),
          ...(route.flowId ? { flow: route.flowId } : {}),
          ...(stepId ? { step: stepId } : {}),
        }),
      );
    },
    [searchParams, pathname, router, route.integrationId, route.flowId],
  );

  const script = useCallback(
    (scriptId: string | null, site?: { stepId?: string | null; jsonPath?: string | null }) => {
      const other = otherParams(searchParams, []);
      const stepId = site?.stepId !== undefined ? site.stepId : route.stepId;
      router.replace(
        buildUrl(pathname, other, {
          surface: "celigo",
          ...(route.integrationId ? { integration: route.integrationId } : {}),
          ...(route.flowId ? { flow: route.flowId } : {}),
          ...(stepId ? { step: stepId } : {}),
          ...(scriptId ? { script: scriptId } : {}),
          // The site only means anything alongside a script, so closing the
          // drawer (`scriptId: null`) drops it rather than stranding a `site`
          // that names nothing.
          ...(scriptId && site?.jsonPath ? { site: site.jsonPath } : {}),
        }),
      );
    },
    [searchParams, pathname, router, route.integrationId, route.flowId, route.stepId],
  );

  const scripts = useCallback(
    (opts?: {
      family?: string | null;
      copy?: string | null;
      in?: string | null;
      filter?: ScriptsFilter;
      kind?: ScriptsKind | null;
      q?: string;
      compare?: ScriptsCompare | null;
    }) => {
      // Same drop list as `go.integrations`/`go.flow`: this is a fresh
      // page, not a continuation of whatever Files-surface state the URL
      // carried. `integration`/`flow`/`step`/`script`/`site`/`view` are
      // never in the object below, so they fall away too even if the
      // caller arrived from a URL that had them (e.g. the integration
      // page's "Open in Scripts view" link, which still has `?integration=`
      // on screen when it calls this).
      const other = otherParams(searchParams, ["file", "workspace"]);
      const o = opts ?? {};
      router.push(
        buildUrl(pathname, other, {
          surface: "celigo",
          tab: "scripts",
          ...(o.family ? { family: o.family } : {}),
          ...(o.copy ? { copy: o.copy } : {}),
          ...(o.compare ? { compare: formatCompare(o.compare) } : {}),
          ...(o.filter && o.filter !== "all" ? { filter: o.filter } : {}),
          ...(o.kind ? { kind: o.kind } : {}),
          ...(o.q ? { q: o.q } : {}),
          ...(o.in ? { in: o.in } : {}),
        }),
      );
    },
    [searchParams, pathname, router],
  );

  return { ...route, go: { files, integrations, integration, tab, view, flow, step, script, scripts } };
}
