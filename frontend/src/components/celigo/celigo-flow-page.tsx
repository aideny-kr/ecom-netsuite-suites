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
import { CeligoFlowHeader, HEADER_FIRST_ROW_FALLBACK_HEIGHT_PX, type ClonedFromInfo } from "./celigo-flow-header";
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
 * 2026-09-04-celigo-flow-sizing.html) -- the header sits in a plain clipped
 * `div` (never a `react-resizable-panels` `Panel`; see the header-fix
 * docstring below for why), with a draggable divider between it and the
 * body and "Focus canvas" as the one-click version. Both persisted per
 * VIEWER, as TWO SEPARATE facts that never derive from each other:
 * - `flowHeaderCollapsed` ("1"/absent) -- toggling it must ALSO shrink/
 *   restore the header's own clipped height and hide/show the divider,
 *   neither of which the header component can reach on its own. The header
 *   stays the single place the toggle BUTTON lives, but is purely
 *   presentational for it (`headerCollapsed`/`onToggleHeaderCollapsed`
 *   props), exactly like the navigator rail's `collapsed`/`onToggle` below.
 * - `flowHeaderHeight` -- a PIXEL count (never a percentage: unlike a
 *   `Panel`'s `%`-of-group sizing, this height is compared directly against
 *   MIN/MAX measurements taken in real pixels) of the height the viewer last
 *   dragged/keyed the divider to. `null` (never stored, a corrupt/legacy
 *   value, or a drag/key that landed ON MAX -- see `clearHeaderHeight`)
 *   means "no preference" -- the applied height then tracks MAX (the
 *   header's own full, unclipped content height) directly, so a first visit
 *   looks exactly like today's page and "all the way down" keeps following
 *   the content as it grows.
 *
 * MIN/MAX fix (the header-sizing bug this replaces): the OLD header lived in
 * a `Panel` sized as a percentage of the vertical group, with a `minSize`
 * floor and NO `maxSize` -- so dragging the divider past the header's own
 * natural content height opened a blank band between the header and the
 * canvas (the header's fixed-height content simply didn't stretch to fill
 * whatever height the panel was given). This wrapper instead measures the
 * header's REAL bounds and never allows an applied height outside them:
 * `headerMinHeight` is `CeligoFlowHeader`'s own first-row height (reported
 * via `onBoundsChange`, `celigo-flow-header.tsx`) and `headerMaxHeight` is
 * its whole natural content height -- so `clampHeaderHeight(min, x, max)`
 * can never produce a height the header itself doesn't fill. */
const FLOW_HEADER_COLLAPSED_KEY = "celigo.flowHeaderCollapsed";
const FLOW_HEADER_HEIGHT_KEY = "celigo.flowHeaderHeight";
/** The retired `%`-based key from before this fix. Never read for a value
 * any more (a percentage of a `Panel` group means nothing against a pixel
 * clamp) -- removed on mount purely so an old browser doesn't keep carrying
 * a value nothing will ever look at again. */
const FLOW_HEADER_LEGACY_SIZE_KEY = "celigo.flowHeaderSize";
/** ArrowUp/ArrowDown step on the divider, matching the mock's own `setH(h ±
 * 24)` and the script drawer's `DRAWER_WIDTH_STEP`. */
const HEADER_KEYBOARD_STEP_PX = 24;

function readStoredHeaderCollapsed(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(FLOW_HEADER_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

/** `null` (never stored, a blank value, or anything unparseable) means "no
 * preference" -- the caller then clamps against MAX itself, which is what
 * makes a first visit show the full header rather than some fixed guess. */
function readStoredHeaderHeight(): number | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(FLOW_HEADER_HEIGHT_KEY);
    const parsed = raw !== null ? Number.parseFloat(raw) : NaN;
    return Number.isFinite(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function persistHeaderHeight(px: number): void {
  try {
    window.localStorage.setItem(FLOW_HEADER_HEIGHT_KEY, String(Math.round(px)));
  } catch {
    // Best effort -- the height still applies for this render, it just
    // won't survive a reload.
  }
}

/** "All the way down" is not a pixel preference. A drag or key that lands
 * ON MAX means "show everything", and everything is a moving target (Show
 * more on the AI description, a longer sibling flow) -- so the preference is
 * cleared instead of frozen at whatever MAX happened to be at that moment,
 * and the applied height goes back to tracking MAX directly. Storing the
 * pixel value here is exactly what would clip the text a later "Show more"
 * reveals. */
function clearHeaderHeight(): void {
  try {
    window.localStorage.removeItem(FLOW_HEADER_HEIGHT_KEY);
  } catch {
    // Best effort -- see `persistHeaderHeight`.
  }
}

/** Clamps to MAX first, then to MIN -- the same order the mock's own
 * `setH` uses (`Math.max(MIN_H, Math.min(maxH(), h))`). MIN wins when the
 * two invert (a MAX that has shrunk below MIN, however briefly mid-remeasure)
 * so the header is never asked to render shorter than its own first row. */
function clampHeaderHeight(min: number, value: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

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

  // "Focus canvas" / the header↔body divider -- see the constants block
  // above for why these two facts are tracked (and persisted) separately.
  const [headerCollapsed, setHeaderCollapsed] = useState<boolean>(readStoredHeaderCollapsed);
  // `null` = no dragged/keyed preference yet -- the applied height then
  // tracks MAX directly (see `appliedHeaderHeight` below), which is what
  // makes a first visit show the full, unclipped header.
  const [storedHeaderHeight, setStoredHeaderHeight] = useState<number | null>(readStoredHeaderHeight);
  // Placeholder seeds only -- `CeligoFlowHeader`'s own layout effect
  // (`onBoundsChange`) overwrites both with a real measurement before the
  // very first paint in a real browser (a `useLayoutEffect` inside a child
  // flushes synchronously before the parent's own paint). jsdom has no real
  // layout engine, so these stay at their seed values in a test that never
  // mocks `scrollHeight`/`getBoundingClientRect` -- harmless, since the
  // resulting fixed height only affects VISUAL clipping, which jsdom does
  // not enforce against `screen.getByText` queries either way.
  const [headerMinHeight, setHeaderMinHeight] = useState<number>(HEADER_FIRST_ROW_FALLBACK_HEIGHT_PX);
  const [headerMaxHeight, setHeaderMaxHeight] = useState<number>(HEADER_FIRST_ROW_FALLBACK_HEIGHT_PX);
  const headerWrapRef = useRef<HTMLDivElement>(null);
  // `latest` is the last clamped height the drag applied, so the drag END can
  // commit it directly (no side effect inside a state updater, which React
  // may invoke twice) -- `null` until the first pointermove.
  const headerDragRef = useRef<{ pointerId: number; startY: number; startHeight: number; latest: number | null } | null>(
    null,
  );
  const [headerDragging, setHeaderDragging] = useState(false);

  const handleHeaderBoundsChange = useCallback((min: number, max: number) => {
    setHeaderMinHeight(min);
    setHeaderMaxHeight(max);
  }, []);

  // The retired `%`-based key means nothing against a pixel clamp -- removed
  // once, on mount, so an old browser's stored value doesn't linger forever
  // pointing at nothing.
  useEffect(() => {
    try {
      window.localStorage.removeItem(FLOW_HEADER_LEGACY_SIZE_KEY);
    } catch {
      // Best effort -- next mount tries again.
    }
  }, []);

  // The header can never be taller than its own content: collapsed pins it
  // to MIN (the first row only); otherwise the stored preference (or MAX,
  // absent one) is clamped into `[headerMinHeight, headerMaxHeight]` on
  // every render -- so a MAX that shrinks (Show less, a shorter flow, the
  // navigator hopping to a sibling with a shorter header) pulls the applied
  // height down with it instead of leaving a blank band below the header.
  const appliedHeaderHeight = headerCollapsed
    ? headerMinHeight
    : clampHeaderHeight(headerMinHeight, storedHeaderHeight ?? headerMaxHeight, headerMaxHeight);

  // The one place a viewer-chosen height is applied AND remembered (drag end,
  // every key): landing on MAX clears the preference (see
  // `clearHeaderHeight`), anything short of it is stored in pixels.
  const commitHeaderHeight = useCallback(
    (next: number) => {
      if (next >= headerMaxHeight) {
        setStoredHeaderHeight(null);
        clearHeaderHeight();
        return;
      }
      setStoredHeaderHeight(next);
      persistHeaderHeight(next);
    },
    [headerMaxHeight],
  );

  const handleHeaderGripPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const startHeight = headerWrapRef.current?.getBoundingClientRect().height ?? appliedHeaderHeight;
      headerDragRef.current = { pointerId: e.pointerId, startY: e.clientY, startHeight, latest: null };
      setHeaderDragging(true);
      e.currentTarget.setPointerCapture?.(e.pointerId);
    },
    [appliedHeaderHeight],
  );

  const handleHeaderGripPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const drag = headerDragRef.current;
      if (!drag || drag.pointerId !== e.pointerId) return;
      // Moving the pointer DOWN (a larger clientY) grows the header -- the
      // mock's own `setH(startH + (e.clientY - startY))`.
      const next = clampHeaderHeight(headerMinHeight, drag.startHeight + (e.clientY - drag.startY), headerMaxHeight);
      drag.latest = next;
      setStoredHeaderHeight(next);
    },
    [headerMinHeight, headerMaxHeight],
  );

  const handleHeaderGripPointerUp = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const drag = headerDragRef.current;
      if (!drag || drag.pointerId !== e.pointerId) return;
      headerDragRef.current = null;
      setHeaderDragging(false);
      e.currentTarget.releasePointerCapture?.(e.pointerId);
      // Committed on drag END, not per-move -- a rapid drag would otherwise
      // hammer localStorage on every pointermove (same reasoning as the
      // script drawer's own grip). A pointerdown with no move commits
      // nothing: the preference stays whatever it was.
      if (drag.latest !== null) commitHeaderHeight(drag.latest);
    },
    [commitHeaderHeight],
  );

  const handleHeaderGripKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      let next: number | null = null;
      if (e.key === "ArrowUp") next = clampHeaderHeight(headerMinHeight, appliedHeaderHeight - HEADER_KEYBOARD_STEP_PX, headerMaxHeight);
      else if (e.key === "ArrowDown") next = clampHeaderHeight(headerMinHeight, appliedHeaderHeight + HEADER_KEYBOARD_STEP_PX, headerMaxHeight);
      else if (e.key === "Home") next = headerMinHeight;
      else if (e.key === "End") next = headerMaxHeight;
      if (next === null) return;
      e.preventDefault();
      // A keyboard move has no separate "end" event to commit from --
      // apply and remember together, unlike the drag handlers above.
      commitHeaderHeight(next);
    },
    [headerMinHeight, headerMaxHeight, appliedHeaderHeight, commitHeaderHeight],
  );

  // No imperative panel resize, no "was that echo my own drag" guard: this
  // header is no longer a `react-resizable-panels` `Panel`, so the whole
  // synthetic-vs-real-drag problem the old `onHeaderResize` guarded against
  // (a library `ResizeObserver` echoing an imperative `resize()` call back
  // as if it were a viewer drag) does not exist for this implementation --
  // `appliedHeaderHeight` above already re-derives the right height purely
  // from `headerCollapsed` on every render, with no separate echo to filter.
  const toggleHeaderCollapsed = useCallback(() => {
    const next = !headerCollapsed;
    try {
      if (next) window.localStorage.setItem(FLOW_HEADER_COLLAPSED_KEY, "1");
      else window.localStorage.removeItem(FLOW_HEADER_COLLAPSED_KEY);
    } catch {
      // Best effort -- the toggle still works for this render, it just
      // won't survive a reload.
    }
    setHeaderCollapsed(next);
  }, [headerCollapsed]);

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
      // A plain flex column: the clipped header wrap, then (unless Focus
      // canvas has hidden it) the draggable divider, then the body -- NOT
      // keyed per flow, unlike the header inside it. The divider's height is
      // a per-VIEWER preference, not a fact about any one flow; keying this
      // wrapper would reset it on every navigator hop the same bug the
      // header's own `key={d.id}` deliberately causes for AI-description
      // expansion (which SHOULD reset) would otherwise cause here too
      // (which should NOT).
      //
      // Never a `react-resizable-panels` `Panel`: a `Panel`'s height is
      // whatever the group hands it, with no way to cap it at the header's
      // own natural content height -- which is exactly how the header-
      // sizing bug this replaces shipped (dragging past that height opened a
      // blank band below the header instead of stopping). `appliedHeaderHeight`
      // (computed above) is already clamped to the header's REAL measured
      // bounds, so the wrapper below can apply it directly.
      <div className="flex flex-1 min-h-0 flex-col">
        <div
          ref={headerWrapRef}
          data-testid="celigo-flow-header-wrap"
          className="overflow-hidden"
          style={{ height: `${appliedHeaderHeight}px` }}
        >
          <CeligoFlowHeader
            // Keyed per flow so the header's own view state (AI description
            // expanded) starts fresh when the navigator hops to a sibling
            // whose detail is already cached -- otherwise the second flow
            // inherited the first one's "Show less". `headerCollapsed` is
            // no longer local state here (see the constants block above),
            // so it survives this remount same as the divider's own height.
            // The remount also re-seeds `headerMinHeight`/`headerMaxHeight`
            // from the NEW flow's own content via `onBoundsChange`'s mount
            // effect -- a shorter sibling's header is never stuck clamped to
            // a taller one's bounds.
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
            onBoundsChange={handleHeaderBoundsChange}
          />
        </div>
        {/* Hidden (unmounted, not just styled away) while Focus canvas has
            collapsed the header -- mirrors the mock's `.divider.hidden`.
            There is nothing to drag a divider FOR when the header is pinned
            to its floor. */}
        {!headerCollapsed && (
          <div
            role="separator"
            aria-orientation="horizontal"
            aria-label="Resize header"
            aria-valuemin={headerMinHeight}
            aria-valuemax={headerMaxHeight}
            aria-valuenow={appliedHeaderHeight}
            tabIndex={0}
            onPointerDown={handleHeaderGripPointerDown}
            onPointerMove={handleHeaderGripPointerMove}
            onPointerUp={handleHeaderGripPointerUp}
            onKeyDown={handleHeaderGripKeyDown}
            className="group relative h-[7px] shrink-0 cursor-row-resize touch-none border-b bg-card focus:outline-none"
          >
            <span
              aria-hidden="true"
              className={cn(
                "absolute left-1/2 top-[2px] h-[3px] w-11 -translate-x-1/2 rounded-full bg-border",
                "group-hover:bg-accent",
                headerDragging && "bg-accent",
              )}
            />
          </div>
        )}
        <div data-testid="celigo-flow-body-pane" className="flex-1 min-h-0">
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
        </div>
      </div>
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
