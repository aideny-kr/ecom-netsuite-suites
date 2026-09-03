"use client";

/**
 * Task 9 — the Celigo surface's root switch.
 *
 * `CeligoSurface` reads the URL (via `useCeligoRoute`) and renders exactly
 * one of three pages: a flow (a flow is selected), an integration (an
 * integration is selected but no flow yet), or the integrations index
 * (neither). All three page components — `CeligoFlowPage` (Task 14),
 * `CeligoIntegrationsPage` (Task 10), `CeligoIntegrationPage` (Task 12) —
 * are defined in their own files and imported here, never inline, so this
 * switch itself never grows page-specific logic.
 *
 * `CeligoBreadcrumb` moved to its own file (`celigo-breadcrumb.tsx`, Task 14,
 * controller ruling R11) — it used to live here, which meant every page
 * importing it created a cycle back through this file's own page imports.
 * Re-exported below so an existing `from "./celigo-surface"` import still
 * resolves.
 */

import { useCeligoRoute } from "./celigo-route";
import { CeligoIntegrationsPage } from "./celigo-integrations-page";
import { CeligoIntegrationPage } from "./celigo-integration-page";
import { CeligoFlowPage } from "./celigo-flow-page";
import { CeligoCommandPalette } from "./celigo-command-palette";

export { CeligoBreadcrumb } from "./celigo-breadcrumb";

/** The Celigo surface's root. Mounted by the workspace page INSTEAD OF the
 * files panel group (never alongside it — see `page.tsx`'s docstring on
 * `surface`), so this is the only path into any Celigo UI. */
export function CeligoSurface(): JSX.Element {
  const route = useCeligoRoute();
  let content: JSX.Element;
  if (route.flowId) {
    content = <CeligoFlowPage />;
  } else if (route.integrationId) {
    content = <CeligoIntegrationPage />;
  } else {
    content = <CeligoIntegrationsPage />;
  }
  return (
    <div data-testid="celigo-surface" className="flex flex-1 min-h-0 flex-col">
      {content}
      {/* Task 11 — mounted once here (not per sub-page) so ⌘K reaches every
          integration and flow regardless of which of the three pages above
          is on screen; it owns its own open state, listening for the
          `celigo:command-k` window event the workspace page dispatches. */}
      <CeligoCommandPalette />
    </div>
  );
}
