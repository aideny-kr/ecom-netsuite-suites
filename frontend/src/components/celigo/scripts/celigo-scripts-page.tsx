"use client";

/**
 * Task 3 — a MINIMAL shell for the account-wide Scripts view (spec §3.3):
 * the "Celigo › Scripts" crumb, the "Scripts" heading, and the three query
 * states (`queryState()`, `lib/query-state.ts` — a pending query is never
 * rendered as empty, an errored one never as loading or a confident "0
 * scripts"). Everything else the mockup shows for this page — the five
 * stat tiles, the search/filter chips, the list|detail split, the
 * not-synced/no-scripts/no-match empty states — is Task 4's job; this
 * exists only so `celigo-surface.tsx`'s new "Flow map | Scripts" toggle has
 * a real destination to mount instead of nothing.
 *
 * Read-only surface, same as every other Celigo page: nothing here runs,
 * edits, deploys, or pushes anything to Celigo.
 */

import { useCeligoScriptFamilies } from "@/hooks/use-celigo-flows";
import { queryState } from "@/lib/query-state";
import { ErrorNotice } from "../shared";
import { useCeligoRoute } from "../celigo-route";
import { CeligoBreadcrumb } from "../celigo-breadcrumb";

export function CeligoScriptsPage(): JSX.Element {
  const route = useCeligoRoute();
  const familiesQuery = useCeligoScriptFamilies();
  const state = queryState(familiesQuery);

  let body: JSX.Element | null = null;
  if (state === "pending") {
    body = (
      <>
        <span className="sr-only">Loading scripts…</span>
        <div aria-hidden className="h-24 w-full animate-pulse rounded-lg bg-muted" />
      </>
    );
  } else if (state === "error") {
    body = (
      <ErrorNotice
        message="Couldn't load script families."
        onRetry={() => familiesQuery.refetch()}
      />
    );
  }
  // `state === "success"` renders no placeholder body of its own here —
  // Task 4 replaces this whole branch with the tiles/list/detail surface.
  // (`familiesQuery.data` is available to it via the same hook call above.)

  return (
    <div data-testid="celigo-scripts-page" className="flex flex-1 min-h-0 flex-col">
      <CeligoBreadcrumb
        items={[{ label: "Celigo", onClick: () => route.go.integrations() }, { label: "Scripts" }]}
      />
      <div className="flex flex-1 min-h-0 flex-col gap-3 overflow-auto p-4">
        <h3 className="text-[20px] font-semibold tracking-tight">Scripts</h3>
        {body}
      </div>
    </div>
  );
}
