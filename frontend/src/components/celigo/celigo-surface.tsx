"use client";

/**
 * Task 9 — the Celigo surface's root switch, plus the one breadcrumb every
 * level of it shares.
 *
 * `CeligoSurface` reads the URL (via `useCeligoRoute`) and renders exactly
 * one of three pages: a flow (a flow is selected), an integration (an
 * integration is selected but no flow yet), or the integrations index
 * (neither). `CeligoIntegrationPage`/`CeligoFlowPage` below are still
 * placeholders — Tasks 12/14 replace each with its real implementation (in
 * its own file, imported here in place of the stub) without touching this
 * switch; Task 10 already did that for `CeligoIntegrationsPage`, which is
 * why it's imported rather than defined here.
 */

import { useCeligoRoute } from "./celigo-route";
import { CeligoIntegrationsPage } from "./celigo-integrations-page";
import { CeligoCommandPalette } from "./celigo-command-palette";

export function CeligoBreadcrumb({
  items,
}: {
  items: { label: string; onClick?: () => void }[];
}): JSX.Element {
  return (
    <div className="flex flex-wrap items-center gap-1.5 border-b bg-card px-4 py-2 text-[12px] text-muted-foreground">
      {items.map((item, i) => (
        <span key={`${item.label}-${i}`} className="flex items-center gap-1.5">
          {i > 0 && <span className="text-muted-foreground/40">›</span>}
          {item.onClick ? (
            <button
              type="button"
              onClick={item.onClick}
              className="hover:text-foreground transition-colors"
            >
              {item.label}
            </button>
          ) : (
            <b className="font-medium text-foreground">{item.label}</b>
          )}
        </span>
      ))}
    </div>
  );
}

// ─── Placeholders — replaced in place by Tasks 12, 14 ────────────────────────

function CeligoIntegrationPage(): JSX.Element {
  const route = useCeligoRoute();
  return (
    <div className="flex flex-1 min-h-0 flex-col">
      <CeligoBreadcrumb
        items={[
          { label: "My integrations", onClick: () => route.go.integrations() },
          { label: route.integrationId ?? "" },
        ]}
      />
      <p className="p-4 text-[13px] text-muted-foreground">Loading…</p>
    </div>
  );
}

function CeligoFlowPage(): JSX.Element {
  const route = useCeligoRoute();
  const integrationId = route.integrationId;
  return (
    <div className="flex flex-1 min-h-0 flex-col">
      <CeligoBreadcrumb
        items={[
          { label: "My integrations", onClick: () => route.go.integrations() },
          ...(integrationId
            ? [{ label: integrationId, onClick: () => route.go.integration(integrationId) }]
            : []),
          { label: route.flowId ?? "" },
        ]}
      />
      <p className="p-4 text-[13px] text-muted-foreground">Loading…</p>
    </div>
  );
}

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
