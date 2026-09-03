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
import { ErrorNotice } from "./shared";
import { useCeligoRoute } from "./celigo-route";
import { CeligoBreadcrumb } from "./celigo-breadcrumb";
import { CeligoFlowHeader, type ClonedFromInfo } from "./celigo-flow-header";
import { CeligoFlowNavigator } from "./celigo-flow-navigator";
import { CeligoFlowCanvas } from "./celigo-flow-canvas";
import { CeligoStepInspector, type InspectorTab } from "./celigo-step-inspector";
import { CeligoScriptDrawer } from "./celigo-script-drawer";

const NO_FLOWS: CeligoFlowSummary[] = [];

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

  const siblingsQuery = useCeligoIntegrationFlows(detail?.integration_id);
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
      <div className="flex flex-1 min-h-0 flex-col">
        <CeligoFlowHeader
          detail={d}
          lastSyncedAt={lastSyncedAt}
          integrationName={integrationLabel}
          integrationCeligoId={integration?.celigo_id ?? null}
          clonedFrom={clonedFrom}
        />
        <div className="flex-1 min-h-0">
          <PanelGroup id="celigo-flow-v1" orientation="horizontal" className="flex h-full w-full">
            <Panel id="celigo-flow-nav" panelRef={navRef} defaultSize={16} minSize={12} collapsible collapsedSize={4}>
              <CeligoFlowNavigator
                flows={siblings}
                currentFlowId={d.id}
                lastSyncedAt={lastSyncedAt}
                collapsed={navCollapsed}
                onToggle={toggleNav}
                onSelect={(id) => route.go.flow(id)}
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
            <Panel id="celigo-flow-inspector" defaultSize={24} minSize={20}>
              <CeligoStepInspector
                detail={d}
                step={selectedStep}
                tab={inspectorTab}
                onTabChange={setInspectorTab}
                lastSyncedAt={lastSyncedAt}
                onOpenScript={(scriptId) => route.go.script(scriptId)}
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
            ? [{ label: integrationLabel || route.integrationId, onClick: () => route.go.integration(route.integrationId!) }]
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
      <CeligoScriptDrawer scriptId={route.scriptId} onClose={() => route.go.script(null)} />
    </div>
  );
}
