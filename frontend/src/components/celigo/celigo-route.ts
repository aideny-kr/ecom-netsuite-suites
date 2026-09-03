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

export type CeligoRoute = {
  surface: "files" | "celigo";
  view: CeligoView;
  integrationId: string | null;
  tab: CeligoTab;
  flowId: string | null;
  stepId: string | null;
  scriptId: string | null;
};

const VALID_VIEWS: readonly CeligoView[] = ["tiles", "list"];
const VALID_TABS: readonly CeligoTab[] = ["flows", "scripts", "errors", "changes"];

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
  return {
    surface: params.get("surface") === "celigo" ? "celigo" : "files",
    view: VALID_VIEWS.includes(viewParam as CeligoView) ? (viewParam as CeligoView) : "tiles",
    integrationId: params.get("integration"),
    tab: VALID_TABS.includes(tabParam as CeligoTab) ? (tabParam as CeligoTab) : "flows",
    flowId: params.get("flow"),
    stepId: params.get("step"),
    scriptId: params.get("script"),
  };
}

// Fixed serialization order, independent of the order params happened to
// arrive in — so two calls that set the same fields always produce byte-
// identical URLs (stable history entries, stable test assertions).
const CELIGO_KEYS = ["surface", "view", "integration", "tab", "flow", "step", "script"] as const;
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
    /** `integrationId` is the integration THIS flow belongs to, and wins over
     * whatever the current page carries. Every caller that knows it must pass
     * it: the ⌘K palette searches across all integrations, so defaulting to
     * the current one opened a flow under an integration that does not
     * contain it (wrong breadcrumb, wrong sibling list). Omit it only where
     * the flow is known to belong to the page already on screen. */
    flow(id: string, integrationId?: string | null): void;
    step(stepId: string | null): void;
    script(scriptId: string | null): void;
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

  const flow = useCallback(
    (id: string, integrationId?: string | null) => {
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
    (scriptId: string | null) => {
      const other = otherParams(searchParams, []);
      router.replace(
        buildUrl(pathname, other, {
          surface: "celigo",
          ...(route.integrationId ? { integration: route.integrationId } : {}),
          ...(route.flowId ? { flow: route.flowId } : {}),
          ...(route.stepId ? { step: route.stepId } : {}),
          ...(scriptId ? { script: scriptId } : {}),
        }),
      );
    },
    [searchParams, pathname, router, route.integrationId, route.flowId, route.stepId],
  );

  return { ...route, go: { files, integrations, integration, flow, step, script } };
}
