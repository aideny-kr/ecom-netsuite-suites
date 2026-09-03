"use client";

/**
 * Task 9's `CeligoBreadcrumb`, moved out of `celigo-surface.tsx` per
 * controller ruling R11 (Task 14): every page that needs it —
 * `celigo-integrations-page.tsx`, `celigo-integration-page.tsx`, and now
 * `celigo-flow-page.tsx` — used to import it FROM `celigo-surface.tsx`,
 * which itself imports all three page components to switch between them.
 * That is an import cycle (page → surface → page); giving the breadcrumb
 * its own file breaks it. `celigo-surface.tsx` re-exports this so any
 * existing `from "./celigo-surface"` import of `CeligoBreadcrumb` still
 * resolves, but every page now imports it directly from here.
 */
export function CeligoBreadcrumb({
  items,
}: {
  /** `skeleton: true` means "this crumb's real NAME hasn't arrived yet" — the
   * query that resolves it is still pending. It renders as a pulsing bar
   * rather than as `label`, because the only stand-in a caller has at that
   * point is the raw id off the URL, and printing that reads as the
   * integration's actual name (finding: codex flow-canvas #3). */
  items: { label: string; onClick?: () => void; skeleton?: boolean }[];
}): JSX.Element {
  return (
    <div className="flex flex-wrap items-center gap-1.5 border-b bg-card px-4 py-2 text-[12px] text-muted-foreground">
      {items.map((item, i) => (
        <span key={`${item.label}-${i}`} className="flex items-center gap-1.5">
          {i > 0 && <span className="text-muted-foreground/40">›</span>}
          {item.skeleton ? (
            <span
              data-testid="celigo-breadcrumb-skeleton"
              className="inline-flex items-center"
              title="Loading the integration name…"
            >
              <span className="sr-only">Loading the integration name…</span>
              <span aria-hidden className="h-3 w-28 animate-pulse rounded bg-muted" />
            </span>
          ) : item.onClick ? (
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
