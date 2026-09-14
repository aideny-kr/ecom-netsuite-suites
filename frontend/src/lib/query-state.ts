/**
 * THE mapping from a TanStack Query result to what the UI is allowed to
 * claim about it. One function, used by every query consumer in the Celigo
 * flow map and script viewer, so no call site carries its own predicate.
 *
 * Why this exists (staging, 2026-09-01): 26 of 36 per-integration flow
 * queries failed at the transport layer and sat in TanStack's
 * `status: "pending"` / `fetchStatus: "paused"` state. `isLoading` is pending
 * AND fetching, so it was FALSE for them; a call site gating on
 * `isLoading || isError` treated each as resolved-with-no-data and rendered
 * "0 flows" for integrations holding 23. The fix was applied to those
 * queries first, and the next review round found the same `isLoading` gate
 * one level up (integrations, sync status). A per-call-site predicate is a
 * stand-in that drifts; the predicate is `status`, and it lives here.
 *
 * - "error":   the query settled with an error. Render an error with retry.
 * - "pending": no data yet, whether fetching, paused for retry, or disabled.
 *              Render loading. NEVER an empty state.
 * - "success": data is authoritative. Only now may the UI say "0", "none",
 *              "never synced".
 */
export type QueryState = "pending" | "error" | "success";

export function queryState(query: { isPending: boolean; isError: boolean }): QueryState {
  if (query.isError) return "error";
  if (query.isPending) return "pending";
  return "success";
}
