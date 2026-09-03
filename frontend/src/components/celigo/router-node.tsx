/**
 * Task 15 — one router node on the canvas (mockup screen 3's `.rnode`):
 * Celigo's own routing facts, read straight off the synced `CeligoRouter` —
 * pass-through chaining to another router, or the fan-out rule (route to
 * the first/all matching branch, by input filters or script) plus branch
 * count. No text here is invented: a pass-through router whose
 * `next_router_id` this canvas can't resolve to a rendered router just says
 * so ("chains onward"), rather than guessing a number.
 *
 * `index` is this router's own 0-based position among the routers the
 * canvas actually renders (`Router {index + 1}`) — the canvas assigns it in
 * the same left-to-right order `computeLayout` lays the router nodes out
 * in, which is the router chain's declared walk order, never a re-sort.
 * `routerIndexById` is that same numbering keyed by router id — the ONLY
 * way a pass-through router's "chains to router N" can name a real number
 * rather than an opaque id, since this component only ever sees its own
 * router, never the full list.
 */

import type { CeligoRouter } from "@/hooks/use-celigo-flows";
import type { LayoutNode } from "./layout";

const ROUTE_TO_LABEL: Record<string, string> = {
  first_matching_branch: "first matching branch",
  all_matching_branches: "all matching branches",
};

const ROUTE_USING_LABEL: Record<string, string> = {
  input_filters: "by input filters",
  script: "by script",
};

export function routerSummary(router: CeligoRouter, routerIndexById: Map<string, number>): string {
  const soleBranch = router.branches.length === 1 ? router.branches[0] : undefined;
  if (soleBranch?.next_router_id) {
    const targetIndex = routerIndexById.get(soleBranch.next_router_id);
    const chainPart = targetIndex !== undefined ? `→ chains to router ${targetIndex + 1}` : "→ chains onward";
    return `pass-through · 1 branch ${chainPart}`;
  }
  const toLabel = (router.route_records_to && ROUTE_TO_LABEL[router.route_records_to]) || "routes on branches";
  const usingLabel = router.route_records_using ? ROUTE_USING_LABEL[router.route_records_using] : null;
  const parts = [toLabel, usingLabel, String(router.branches.length)].filter((p): p is string => !!p);
  return parts.join(" · ");
}

export function RouterNode({
  router,
  node,
  index,
  routerIndexById,
}: {
  router: CeligoRouter;
  node: Pick<LayoutNode, "x" | "y" | "w">;
  index: number;
  routerIndexById: Map<string, number>;
}): JSX.Element {
  return (
    <div
      data-testid={`router-node-${router.id}`}
      style={{ position: "absolute", left: node.x, top: node.y, width: node.w }}
      className="rounded-full border bg-card px-2.5 py-1.5 text-center shadow-soft"
    >
      <div data-testid="router-label" className="text-[9px] uppercase tracking-wide text-muted-foreground">
        {`Router ${index + 1}`}
      </div>
      <div data-testid="router-value" className="text-[10px] leading-snug text-foreground">
        {routerSummary(router, routerIndexById)}
      </div>
    </div>
  );
}
