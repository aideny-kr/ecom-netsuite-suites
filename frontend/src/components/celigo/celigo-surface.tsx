"use client";

/**
 * Task 9 — the Celigo surface's root switch. Task 3 adds the "Flow map |
 * Scripts" segmented toggle (spec §3.2) on top of the original switch.
 *
 * `CeligoSurface` reads the URL (via `useCeligoRoute`) and renders exactly
 * one of four pages: the account-wide Scripts view (`isScriptsView(route)`
 * — `?tab=scripts` with no `integration` and no `flow`), a flow (a flow is
 * selected), an integration (an integration is selected but no flow yet),
 * or the integrations index (none of the above). Scripts is checked FIRST
 * but only actually wins when neither an integration nor a flow is also on
 * the URL — `integration=X&tab=scripts` stays the integration page's OWN
 * Scripts tab (spec §3.1), which is exactly what `isScriptsView` encodes,
 * so this switch asks it rather than re-deriving the condition inline.
 * `CeligoFlowPage` (Task 14), `CeligoIntegrationsPage` (Task 10),
 * `CeligoIntegrationPage` (Task 12), `CeligoScriptsPage` (Task 3) are
 * defined in their own files and imported here, never inline, so this
 * switch itself never grows page-specific logic.
 *
 * `CeligoBreadcrumb` moved to its own file (`celigo-breadcrumb.tsx`, Task 14,
 * controller ruling R11) — it used to live here, which meant every page
 * importing it created a cycle back through this file's own page imports.
 * Re-exported below so an existing `from "./celigo-surface"` import still
 * resolves.
 */

import { useCeligoRoute, isScriptsView } from "./celigo-route";
import { CeligoIntegrationsPage } from "./celigo-integrations-page";
import { CeligoIntegrationPage } from "./celigo-integration-page";
import { CeligoFlowPage } from "./celigo-flow-page";
import { CeligoCommandPalette } from "./celigo-command-palette";
import { CeligoScriptsPage } from "./scripts/celigo-scripts-page";
import { cn } from "@/lib/utils";

export { CeligoBreadcrumb } from "./celigo-breadcrumb";

/** The "Flow map | Scripts" segmented control (spec §3.2) — same
 * `role="group"` + `aria-pressed` shape as the outer Files|Celigo toggle
 * (`app/(dashboard)/workspace/surface-toggle.tsx`) so a screen-reader user
 * gets the same affordance one level down. "Flow map" always goes to the
 * integrations index rather than trying to remember a prior flow-map
 * location (mockup: "not required"); "Scripts" always lands on the plain
 * Scripts view (no stale family/filter carried over — `go.scripts` replaces
 * the whole scripts-param set, never merges).
 *
 * Residual fix 4 (build judge): `route` is a PROP here, not a second
 * `useCeligoRoute()` call — `CeligoSurface` below already reads the route
 * once (to compute `scriptsActive` via `isScriptsView`) and passes it down,
 * so this toggle's `route.go.*` calls share that same instance instead of
 * re-deriving an independent one from `useSearchParams()`. */
function CeligoTopToggle({
  route,
  scriptsActive,
}: {
  route: ReturnType<typeof useCeligoRoute>;
  scriptsActive: boolean;
}): JSX.Element {
  return (
    <div
      role="group"
      aria-label="Flow map or Scripts"
      className="flex items-center gap-0.5 self-start rounded border p-0.5"
    >
      <button
        type="button"
        onClick={() => route.go.integrations()}
        aria-pressed={!scriptsActive}
        className={cn(
          "rounded px-2 py-0.5 text-[11px] transition-colors",
          !scriptsActive
            ? "bg-accent text-foreground"
            : "text-muted-foreground hover:text-foreground hover:bg-accent/50",
        )}
      >
        Flow map
      </button>
      <button
        type="button"
        onClick={() => route.go.scripts()}
        aria-pressed={scriptsActive}
        className={cn(
          "rounded px-2 py-0.5 text-[11px] transition-colors",
          scriptsActive
            ? "bg-accent text-foreground"
            : "text-muted-foreground hover:text-foreground hover:bg-accent/50",
        )}
      >
        Scripts
      </button>
    </div>
  );
}

/** The Celigo surface's root. Mounted by the workspace page INSTEAD OF the
 * files panel group (never alongside it — see `page.tsx`'s docstring on
 * `surface`), so this is the only path into any Celigo UI. */
export function CeligoSurface(): JSX.Element {
  const route = useCeligoRoute();
  const scriptsActive = isScriptsView(route);
  let content: JSX.Element;
  if (scriptsActive) {
    content = <CeligoScriptsPage />;
  } else if (route.flowId) {
    content = <CeligoFlowPage />;
  } else if (route.integrationId) {
    content = <CeligoIntegrationPage />;
  } else {
    content = <CeligoIntegrationsPage />;
  }
  return (
    <div data-testid="celigo-surface" className="flex flex-1 min-h-0 flex-col">
      <div className="flex items-center px-4 pt-2">
        <CeligoTopToggle route={route} scriptsActive={scriptsActive} />
      </div>
      {content}
      {/* Task 11 — mounted once here (not per sub-page) so ⌘K reaches every
          integration and flow regardless of which of the three pages above
          is on screen; it owns its own open state, listening for the
          `celigo:command-k` window event the workspace page dispatches. */}
      <CeligoCommandPalette />
    </div>
  );
}
