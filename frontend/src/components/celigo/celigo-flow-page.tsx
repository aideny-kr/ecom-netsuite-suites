"use client";

/**
 * Task 14 — the flow page shell (mockup screen 3): the panel group
 * (navigator rail · canvas · inspector) inside the workspace's own resizable
 * layout, the header, and every state that isn't "render the real canvas" —
 * loading, a failed fetch, an unknown flow id, an empty flow, a paused flow.
 * Replaces the Task 9 stub of the same name in `celigo-surface.tsx`.
 *
 * The canvas (Task 15) and the inspector (Task 16) are still stubs
 * (`celigo-flow-canvas.tsx` / `celigo-step-inspector.tsx`) — this file wires
 * their FINAL prop contracts so neither task touches this one when it lands.
 *
 * Every query gates through `queryState()` (`lib/query-state.ts`), same as
 * every other Celigo page — a pending query is never rendered as empty, and
 * an errored one is never rendered as loading or as a fabricated "0 flows".
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Panel,
  Group as PanelGroup,
  Separator as PanelResizeHandle,
  type PanelImperativeHandle,
  type PanelSize,
} from "react-resizable-panels";
import {
  useCeligoFlowDetail,
  useCeligoIntegrationFlows,
  useCeligoIntegrations,
  useCeligoSyncStatus,
  type CeligoFlowStep,
  type CeligoFlowSummary,
} from "@/hooks/use-celigo-flows";
import { queryState } from "@/lib/query-state";
import { cn } from "@/lib/utils";
import { ErrorNotice } from "./shared";
import { useCeligoRoute } from "./celigo-route";
import { CeligoBreadcrumb } from "./celigo-breadcrumb";
import { CeligoFlowHeader, type ClonedFromInfo } from "./celigo-flow-header";
import { CeligoFlowNavigator } from "./celigo-flow-navigator";
import { CeligoFlowCanvas } from "./celigo-flow-canvas";
import { CeligoStepInspector, type InspectorTab } from "./celigo-step-inspector";
import { CeligoScriptDrawer } from "./celigo-script-drawer";
import { isCeligoPaletteOpen } from "./palette-open-state";

const NO_FLOWS: CeligoFlowSummary[] = [];

/** The navigator panel's three sizes, as ONE set of numbers. They are also
 * the threshold `onNavResize` compares against — a collapsible panel is
 * either at least `NAV_MIN_PCT` wide or snapped all the way to
 * `NAV_COLLAPSED_PCT`, with nothing in between — so keeping the props and the
 * threshold as separate literals would let a resize of the panel drift out of
 * agreement with what `navCollapsed` believes. Percentage STRINGS on the
 * props: `react-resizable-panels` 4.6.4 reads a bare number as PIXELS. */
const NAV_MIN_PCT = 12;
const NAV_COLLAPSED_PCT = 4;
const NAV_DEFAULT_SIZE = "16%";
const NAV_MIN_SIZE = `${NAV_MIN_PCT}%`;
const NAV_COLLAPSED_SIZE = `${NAV_COLLAPSED_PCT}%`;

/** Celigo flow sizing UI (approved mock: docs/superpowers/mockups/
 * 2026-09-04-celigo-flow-sizing.html) -- header + canvas are now a VERTICAL
 * `PanelGroup`, with a draggable divider between them and "Focus canvas" as
 * the one-click version. Both persisted per VIEWER, as TWO SEPARATE facts
 * that never derive from each other:
 * - `flowHeaderCollapsed` ("1"/absent) -- moved here from
 *   `celigo-flow-header.tsx` (which used to own it entirely) because
 *   toggling it must ALSO shrink/restore the real header PANEL and hide/
 *   show the divider, neither of which the header component can reach on
 *   its own. The header stays the single place the toggle BUTTON lives, but
 *   is now purely presentational for it (`headerCollapsed`/
 *   `onToggleHeaderCollapsed` props), exactly like the navigator rail's
 *   `collapsed`/`onToggle` below.
 * - `flowHeaderSize` (a percentage STRING of the vertical group -- v4 reads
 *   a bare number as PIXELS, same caveat as `NAV_DEFAULT_SIZE` above) --
 *   the height the viewer last DRAGGED the divider to. `minSize` is a small
 *   floor (not the first row's real height, which isn't knowable
 *   statically) -- the header's own `overflow-hidden` plus its
 *   `!headerCollapsed` conditional rendering keeps the first row (pills,
 *   title, actions) visible at any height above that floor.
 */
const FLOW_HEADER_COLLAPSED_KEY = "celigo.flowHeaderCollapsed";
const FLOW_HEADER_SIZE_KEY = "celigo.flowHeaderSize";
const FLOW_HEADER_DEFAULT_SIZE = "34%";
const FLOW_HEADER_MIN_SIZE = "8%";
/** `onResize` persistence is debounced by this many ms so a drag doesn't
 * hammer `localStorage` on every pointermove -- matches the mock's own
 * "written on drag end" for the drawer's width, loosened slightly here
 * since the library's `onResize` (a real `ResizeObserver`, unlike the
 * drawer's own hand-rolled pointer handlers) has no separate "drag end"
 * event to hook. */
const FLOW_HEADER_RESIZE_DEBOUNCE_MS = 200;

function readStoredHeaderCollapsed(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(FLOW_HEADER_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function readStoredHeaderSize(): string {
  if (typeof window === "undefined") return FLOW_HEADER_DEFAULT_SIZE;
  try {
    const stored = window.localStorage.getItem(FLOW_HEADER_SIZE_KEY);
    // A bare number (or anything else un-"%"-suffixed) would mean PIXELS to
    // the library -- reject it rather than hand it a value that silently
    // means something else.
    return stored && /%$/.test(stored) ? stored : FLOW_HEADER_DEFAULT_SIZE;
  } catch {
    return FLOW_HEADER_DEFAULT_SIZE;
  }
}

function persistHeaderSize(size: string): void {
  try {
    window.localStorage.setItem(FLOW_HEADER_SIZE_KEY, size);
  } catch {
    // Best effort -- the size still applies for this render, it just won't
    // survive a reload.
  }
}

/** Parses a percentage STRING ("34%") down to its bare number (34), for
 * comparing against `PanelSize.asPercentage`. Never throws -- an
 * unparseable size (shouldn't happen; every caller here is one of our own
 * constants or `readStoredHeaderSize`'s own "%"-suffix-checked output)
 * reads as 0 rather than `NaN`, which would otherwise make every
 * comparison against it false and silently defeat the guard below. */
function percentValue(size: string): number {
  const value = Number.parseFloat(size);
  return Number.isFinite(value) ? value : 0;
}

/** How close a reported `asPercentage` has to land to a just-requested
 * imperative target to be treated as THAT request's own report rather than
 * a genuine drag -- a `resize()` call and the `ResizeObserver` notification
 * it produces don't always round to the identical fraction (subpixel
 * layout rounding), so an exact `===` would occasionally misclassify the
 * request's own echo as a drag. */
const HEADER_RESIZE_MATCH_EPSILON = 0.5;

/** `queryState()` only tells us the query settled with an error — it
 * doesn't say which one. The unknown-id state ("This flow is not in the
 * last sync.") needs to tell a 404 (the id genuinely isn't in the last
 * sync) apart from every other failure (network, 500, auth), which needs
 * the STATUS code, not just "errored".
 *
 * Review-fix (Task 14, finding #1): this used to ALSO fall back to a
 * `/\b404\b/` regex over `error.message`, on the belief that `apiClient`'s
 * `request()` threw a bare `Error` with no status at all. That fallback was
 * worse than useless: the backend always overwrites the thrown message with
 * its own `detail` text (e.g. `{"detail": "Flow not found"}`, never
 * containing the literal "404"), so the regex could never match a REAL
 * 404 — and it could false-POSITIVE on an unrelated failure whose message
 * happened to contain that number. `request()` now throws `ApiError`
 * (`lib/api-client.ts`), which carries the real HTTP status on `.status` —
 * so status is the only thing this checks. */
function is404(error: unknown): boolean {
  return !!(error && typeof error === "object" && (error as { status?: unknown }).status === 404);
}

function PageSkeleton(): JSX.Element {
  return (
    <>
      <span className="sr-only">Loading flow…</span>
      <div aria-hidden="true" className="flex flex-col gap-3 p-4">
        <div className="h-6 w-96 animate-pulse rounded bg-muted" />
        <div className="h-4 w-64 animate-pulse rounded bg-muted" />
        <div className="h-64 animate-pulse rounded-xl border bg-card" />
      </div>
    </>
  );
}

export function CeligoFlowPage(): JSX.Element {
  const route = useCeligoRoute();
  const flowId = route.flowId ?? undefined;

  const detailQuery = useCeligoFlowDetail(flowId);
  const detailState = queryState(detailQuery);
  const detail = detailState === "success" ? detailQuery.data! : undefined;

  // The flow's OWN integration wins once the detail lands; the URL's
  // `?integration=` is only a head start, so the sibling list can begin
  // loading in parallel with the detail instead of strictly after it. The
  // order matters: a stale or hand-edited `?integration=` must not decide
  // which flows the navigator lists, so `detail` overrides it the moment it
  // arrives (React Query simply refetches under the corrected key).
  const siblingsIntegrationId = detail?.integration_id ?? route.integrationId ?? undefined;
  const siblingsQuery = useCeligoIntegrationFlows(siblingsIntegrationId);
  const siblingsState = queryState(siblingsQuery);
  const siblings = siblingsState === "success" ? siblingsQuery.data! : NO_FLOWS;

  const integrationsQuery = useCeligoIntegrations();
  const integrationsState = queryState(integrationsQuery);
  const integration =
    integrationsState === "success" && detail
      ? integrationsQuery.data!.find((i) => i.id === detail.integration_id)
      : undefined;

  const syncStatusQuery = useCeligoSyncStatus();
  const syncStatusState = queryState(syncStatusQuery);
  const lastSyncedAt = syncStatusState === "success" ? syncStatusQuery.data?.last_synced_at ?? null : null;

  const navRef = useRef<PanelImperativeHandle>(null);
  const [navCollapsed, setNavCollapsed] = useState(true);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("facts");

  // "Focus canvas" / the header↔canvas divider -- see the constants block
  // above for why these two facts are tracked (and persisted) separately.
  const headerPanelRef = useRef<PanelImperativeHandle>(null);
  const [headerCollapsed, setHeaderCollapsed] = useState<boolean>(readStoredHeaderCollapsed);
  const [headerSize, setHeaderSize] = useState<string>(readStoredHeaderSize);
  // Guards the imperative `resize()` calls `toggleHeaderCollapsed` makes
  // below from being read back by `onHeaderResize` as the VIEWER'S own drag
  // -- without it, collapsing (or restoring) the panel would immediately
  // overwrite `flowHeaderSize` with the collapsed height, and the very next
  // "Show details" would restore to that instead of what the viewer
  // actually dragged to.
  //
  // Gate-fix (finding: blocker): this used to be a BOOLEAN cleared on a
  // microtask (`Promise.resolve().then()`), on the assumption that clearing
  // it "one tick later" would still be up when the resulting notification
  // arrived. It never was: `onResize` is driven by a real `ResizeObserver`,
  // and a ResizeObserver's notifications are delivered as part of the
  // browser's rendering step -- strictly AFTER the microtask queue drains,
  // not as one more microtask in it. So by the time the real notification
  // landed, the guard had already cleared itself, and it protected nothing.
  // Invisible in this file's own tests only because `vitest.setup.ts` stubs
  // `ResizeObserver` as a no-op (added for reactflow) -- the exact path this
  // guard exists for never fires under vitest.
  //
  // The fix drops timing entirely: remember the exact percentage the
  // imperative call just requested, and compare the REPORTED percentage
  // against that value (not against a clock) when `onResize` runs. A match
  // is this call's own echo, however late it lands; anything else is a
  // genuine drag.
  const pendingHeaderResizeTargetRef = useRef<number | null>(null);
  const headerResizeDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (headerResizeDebounceRef.current) clearTimeout(headerResizeDebounceRef.current);
    };
  }, []);

  const onHeaderResize = useCallback((panelSize: PanelSize) => {
    const pendingTarget = pendingHeaderResizeTargetRef.current;
    if (pendingTarget !== null) {
      pendingHeaderResizeTargetRef.current = null;
      if (Math.abs(panelSize.asPercentage - pendingTarget) <= HEADER_RESIZE_MATCH_EPSILON) return;
    }
    const size = `${panelSize.asPercentage}%`;
    setHeaderSize(size);
    if (headerResizeDebounceRef.current) clearTimeout(headerResizeDebounceRef.current);
    headerResizeDebounceRef.current = setTimeout(() => persistHeaderSize(size), FLOW_HEADER_RESIZE_DEBOUNCE_MS);
  }, []);

  const toggleHeaderCollapsed = useCallback(() => {
    setHeaderCollapsed((prev) => {
      const next = !prev;
      try {
        if (next) window.localStorage.setItem(FLOW_HEADER_COLLAPSED_KEY, "1");
        else window.localStorage.removeItem(FLOW_HEADER_COLLAPSED_KEY);
      } catch {
        // Best effort -- the toggle still works for this render, it just
        // won't survive a reload.
      }
      const targetSize = next ? FLOW_HEADER_MIN_SIZE : headerSize;
      pendingHeaderResizeTargetRef.current = percentValue(targetSize);
      headerPanelRef.current?.resize(targetSize);
      return next;
    });
  }, [headerSize]);

  // The element that opened the script drawer, so Radix can hand focus back
  // when it closes. Nothing else can supply it: the drawer is mounted here,
  // the button lives inside the inspector, and only the CLICK knows which of
  // several "Open source →" buttons it was. Without it Radix restored to
  // nothing and focus fell to <body> — a keyboard reader who opened a script
  // from deep in the inspector restarted at the top of the page.
  const scriptOpenerRef = useRef<HTMLElement | null>(null);

  // The navigator starts collapsed as a rail (mockup: "Navigator rail ·
  // ⌘B expands"). `navCollapsed` is the source of truth for what
  // `CeligoFlowNavigator` renders; the imperative calls below additionally
  // keep the REAL panel's pixel width in sync for an actual browser (jsdom
  // has no layout to reflect this either way, so tests assert the rendered
  // rail/list, not the panel's geometry).
  useEffect(() => {
    navRef.current?.collapse();
  }, []);

  const toggleNav = useCallback(() => {
    setNavCollapsed((prev) => {
      const next = !prev;
      if (next) navRef.current?.collapse();
      else navRef.current?.expand();
      return next;
    });
  }, []);

  // The toggle above is not the only way this panel collapses: dragging the
  // separator past `minSize` collapses it too, and dragging back out expands
  // it — neither goes through `toggleNav`, so `navCollapsed` used to drift
  // out of sync with the real panel. The visible result was the whole named
  // flow list squeezed into a 4%-wide rail (or a rail's worth of dots
  // stranded in a full-width panel). `onResize` is the panel's own report of
  // what it actually did (v4 exposes no onCollapse/onExpand pair), so state
  // follows geometry instead of guessing at it.
  const onNavResize = useCallback((panelSize: PanelSize) => {
    setNavCollapsed(panelSize.asPercentage < NAV_MIN_PCT);
  }, []);

  useEffect(() => {
    function onToggleNav() {
      toggleNav();
    }
    window.addEventListener("celigo:toggle-nav", onToggleNav);
    return () => window.removeEventListener("celigo:toggle-nav", onToggleNav);
  }, [toggleNav]);

  // Esc clears the current selection, topmost layer first: a script drawer
  // open over a selected step closes the drawer on the first Esc and only
  // clears the step on a second one (mockup: "Esc clears the selection; a
  // second Esc closes the drawer" — read top-down, drawer-over-step, since
  // `route.go.script` never sets a `script` param without a `step` already
  // selected, see `celigo-route.ts`).
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== "Escape") return;
      // The ⌘K palette is dismissed by Radix from a document-level CAPTURE
      // listener that does not stop the event, so this window listener runs on
      // the same keypress. Without this guard, dismissing the palette also
      // cleared the step selected on the page behind it (finding I5).
      if (isCeligoPaletteOpen()) return;
      if (route.scriptId) {
        route.go.script(null);
        return;
      }
      if (route.stepId) {
        route.go.step(null);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [route.scriptId, route.stepId, route.go]);

  const selectedStep: CeligoFlowStep | null = useMemo(() => {
    if (!detail || !route.stepId) return null;
    return detail.steps.find((s) => s.id === route.stepId) ?? null;
  }, [detail, route.stepId]);

  const clonedFrom: ClonedFromInfo | null = useMemo(() => {
    // Review-fix (Task 14, finding #2): also withhold the fact while the
    // siblings query has ERRORED, not just while it's pending. An errored
    // `useCeligoIntegrationFlows` collapses to the same empty `siblings`
    // array used for "no data yet" (see `NO_FLOWS` above) — without this
    // guard that reads as a confirmed negative ("cloned from a flow no
    // longer in the account") even though the real sibling that would
    // resolve the name may exist and simply failed to load. A failed
    // request must render differently from both loading and a genuine
    // absence.
    if (!detail?.source_id || siblingsState === "pending" || siblingsState === "error") return null;
    return { resolvedName: siblings.find((f) => f.celigo_id === detail.source_id)?.name ?? null };
  }, [detail, siblings, siblingsState]);

  const integrationLabel = integration?.name ?? route.integrationId ?? "";
  const flowLabel = detail?.name ?? route.flowId ?? "";

  let body: JSX.Element;
  if (detailState === "pending") {
    body = <PageSkeleton />;
  } else if (detailState === "error") {
    if (is404(detailQuery.error)) {
      body = (
        <div className="flex flex-col items-start gap-2 p-4 text-[13px] text-muted-foreground">
          <p>This flow is not in the last sync.</p>
          <button
            type="button"
            className="font-medium text-foreground underline"
            onClick={() =>
              route.integrationId ? route.go.integration(route.integrationId) : route.go.integrations()
            }
          >
            {route.integrationId ? "Back to the integration" : "Back to My integrations"}
          </button>
        </div>
      );
    } else {
      body = (
        <div className="p-4">
          <ErrorNotice message="Couldn't load this flow." onRetry={() => detailQuery.refetch()} />
        </div>
      );
    }
  } else {
    // detailState === "success"
    const d = detail!;
    const paused = d.disabled === true;
    const hasSteps = d.steps.length > 0;

    body = (
      // The vertical group: header pane, then (unless Focus canvas has
      // hidden it) the draggable divider, then the body pane -- NOT keyed
      // per flow, unlike the header inside it. The divider's size is a
      // per-VIEWER preference, not a fact about any one flow; keying this
      // group would reset it on every navigator hop the same bug the
      // header's own `key={d.id}` deliberately causes for AI-description
      // expansion (which SHOULD reset) would otherwise cause here too
      // (which should NOT).
      <PanelGroup id="celigo-flow-vertical" orientation="vertical" className="flex flex-1 min-h-0 flex-col">
        <Panel
          id="celigo-flow-header-pane"
          panelRef={headerPanelRef}
          defaultSize={headerSize}
          minSize={FLOW_HEADER_MIN_SIZE}
          onResize={onHeaderResize}
          className="overflow-hidden"
        >
          <CeligoFlowHeader
            // Keyed per flow so the header's own view state (AI description
            // expanded) starts fresh when the navigator hops to a sibling
            // whose detail is already cached -- otherwise the second flow
            // inherited the first one's "Show less". `headerCollapsed` is
            // no longer local state here (see the constants block above),
            // so it survives this remount same as the divider's own size.
            key={d.id}
            detail={d}
            lastSyncedAt={lastSyncedAt}
            syncStatusState={syncStatusState}
            onRetrySyncStatus={() => syncStatusQuery.refetch()}
            integrationName={integrationLabel}
            integrationCeligoId={integration?.celigo_id ?? null}
            clonedFrom={clonedFrom}
            integrationNotice={
              integrationsState === "error" ? (
                <ErrorNotice
                  message="Couldn't load this integration."
                  onRetry={() => integrationsQuery.refetch()}
                />
              ) : null
            }
            headerCollapsed={headerCollapsed}
            onToggleHeaderCollapsed={toggleHeaderCollapsed}
          />
        </Panel>
        {/* Hidden (unmounted, not just styled away) while Focus canvas has
            collapsed the header -- mirrors the mock's `.divider.hidden`.
            There is nothing to drag a divider FOR when the panel it borders
            is pinned to its floor. */}
        {!headerCollapsed && (
          <PanelResizeHandle
            id="celigo-flow-header-divider"
            aria-label="Resize header"
            className={cn(
              "relative h-[7px] shrink-0 cursor-row-resize border-b bg-card",
              // The library's own hover/active state, exposed as
              // `data-separator="hover"` / `"active"` on this element
              // (never a class) — the mock's own `:hover, .active`.
              "before:absolute before:left-1/2 before:top-[2px] before:h-[3px] before:w-11 before:-translate-x-1/2 before:rounded-full before:bg-border",
              "data-[separator=hover]:before:bg-accent data-[separator=active]:before:bg-accent",
            )}
          />
        )}
        <Panel id="celigo-flow-body-pane" minSize="20%">
          <PanelGroup id="celigo-flow-v1" orientation="horizontal" className="flex h-full w-full">
            {/* Sizes are PERCENTAGE STRINGS, never bare numbers:
                `react-resizable-panels` 4.6.4 parses a number as PIXELS
                (its size parser is `case "number": return [e, "px"]`) and
                only a "%"-suffixed string as a fraction of the group. As
                numbers these read as a 16px navigator and a 24px inspector
                — invisible slivers — instead of the 16%/24% intended. */}
            <Panel
              id="celigo-flow-nav"
              panelRef={navRef}
              defaultSize={NAV_DEFAULT_SIZE}
              minSize={NAV_MIN_SIZE}
              collapsible
              collapsedSize={NAV_COLLAPSED_SIZE}
              onResize={onNavResize}
            >
              <CeligoFlowNavigator
                flows={siblings}
                state={siblingsState}
                onRetry={() => siblingsQuery.refetch()}
                currentFlowId={d.id}
                lastSyncedAt={lastSyncedAt}
                collapsed={navCollapsed}
                onToggle={toggleNav}
                onSelect={(id) => route.go.flow(id, d.integration_id)}
              />
            </Panel>
            <PanelResizeHandle className="w-px bg-border" />
            <Panel id="celigo-flow-canvas-pane" className="flex-1">
              <div data-testid="celigo-canvas-host" data-paused={paused ? "true" : undefined} className="flex h-full flex-col">
                {paused && (
                  <div className="border-b bg-muted/40 px-3 py-1.5 text-[11px] text-muted-foreground">
                    This flow is Off in Celigo — mirrored here, not changeable here.
                  </div>
                )}
                <div className="flex-1 min-h-0">
                  {hasSteps ? (
                    <CeligoFlowCanvas
                      detail={d}
                      selectedStepId={route.stepId}
                      onSelectStep={(stepId, tab) => {
                        route.go.step(stepId);
                        setInspectorTab(tab ?? "facts");
                      }}
                      paused={paused}
                    />
                  ) : (
                    <p className="flex h-full items-center justify-center text-[13px] text-muted-foreground">
                      No steps recorded for this flow in the last sync.
                    </p>
                  )}
                </div>
              </div>
            </Panel>
            <PanelResizeHandle className="w-px bg-border" />
            <Panel id="celigo-flow-inspector" defaultSize="24%" minSize="20%">
              <CeligoStepInspector
                detail={d}
                step={selectedStep}
                tab={inspectorTab}
                onTabChange={setInspectorTab}
                errorsCheckedAt={d.errors_checked_at}
                onOpenScript={(scriptId, opener, jsonPath) => {
                  scriptOpenerRef.current = opener;
                  // The clicked SITE travels on the URL, so the drawer names
                  // the attachment the reader opened rather than whichever
                  // one the backend returned first — and a pasted link
                  // reopens that same site.
                  route.go.script(scriptId, { jsonPath });
                }}
              />
            </Panel>
          </PanelGroup>
        </Panel>
      </PanelGroup>
    );
  }

  return (
    <div data-testid="celigo-flow-page" className="flex flex-1 min-h-0 flex-col">
      <CeligoBreadcrumb
        items={[
          { label: "My integrations", onClick: () => route.go.integrations() },
          ...(route.integrationId
            ? [
                {
                  label: integrationLabel || route.integrationId,
                  onClick: () => route.go.integration(route.integrationId!),
                  // While the integrations list is still in flight the only
                  // stand-in available is the raw id off the URL, and
                  // printing that reads as the integration's real name. A
                  // skeleton says "the name is coming" instead of asserting
                  // a name that is actually a UUID.
                  skeleton: integrationsState === "pending",
                },
              ]
            : []),
          { label: flowLabel },
        ]}
      />
      {body}
      {/* Task 17 -- the script drawer (mockup screen 4), reached via
          `&script=<scriptId>`. Always mounted (`open={!!scriptId}` inside
          the drawer itself controls visibility via Radix's own Presence)
          rather than conditionally rendered here, so its own Escape/focus-
          restore lifecycle behaves exactly like `CeligoScriptViewerDialog`'s
          already does. Escape ORDERING ("drawer first, a second Escape then
          clears the step") is owned entirely by this file's own `keydown`
          listener above -- this file does not duplicate it. */}
      <CeligoScriptDrawer
        scriptId={route.scriptId}
        currentStepId={route.stepId}
        currentJsonPath={route.scriptSite}
        returnFocusTo={scriptOpenerRef}
        onClose={() => route.go.script(null)}
      />
    </div>
  );
}
