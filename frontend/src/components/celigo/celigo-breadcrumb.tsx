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
