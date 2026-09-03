import { vi } from "vitest";

/**
 * Task 10 — shared TanStack v5 query-result fixtures, for every Celigo test
 * from this task onward (imported instead of each test file re-declaring its
 * own copy). Reproduced from `celigo-flow-map.test.tsx`'s private
 * `resolved`/`pending`/`errored` helpers of the same name and shape — see
 * `shared.tsx`'s top docstring for why a genuine reproduction, not an
 * import, is the right call here too: `celigo-flow-map.tsx` (and its test)
 * are deleted in Task 18 once the new pages replace them, so importing from
 * that file's test would be a dependency on code with a known expiry date.
 *
 * `queryState()` (`lib/query-state.ts`) is the ONE predicate every call site
 * gates on — never `isLoading`. These three fixtures exist because
 * `isLoading` (pending AND fetching) went FALSE for 26 of 36 real queries
 * that were paused mid-retry, and a call site gating on it rendered "0
 * flows" for integrations holding dozens — see `query-state.ts`'s docstring
 * for the incident. `pending()` sets `isLoading: false` deliberately, so a
 * component that regresses to the old predicate fails these fixtures loudly
 * instead of quietly passing on real (never-paused) data.
 */

/** A RESOLVED query, shaped like TanStack v5's result: `status: "success"`
 * with `isPending`/`isError` false. */
export function resolved<T>(data: T) {
  return {
    data,
    status: "success" as const,
    isPending: false,
    isLoading: false,
    isError: false,
    isSuccess: true,
    refetch: vi.fn(),
  };
}

/** A query with no data yet: fetching, paused for retry, or disabled —
 * `status: "pending"`. */
export function pending() {
  return {
    data: undefined,
    status: "pending" as const,
    isPending: true,
    isLoading: false,
    isError: false,
    isSuccess: false,
    refetch: vi.fn(),
  };
}

/** A query that settled with an error. `refetch` defaults to a fresh spy so
 * a caller can pass its own and assert on it (a Retry button wiring test). */
export function errored(refetch = vi.fn()) {
  return {
    data: undefined,
    status: "error" as const,
    isPending: false,
    isLoading: false,
    isError: true,
    isSuccess: false,
    refetch,
  };
}
