# Analytics source routing independent review

Reviewed base `c1ca948e` against the uncommitted worktree at `/Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/fix-analytics-source-routing`. No application files edited; no deployment or external writes performed.

## Status after targeted fix validation

All three findings below are resolved in the current worktree. Independently reran the original lifecycle reproductions: Plan Mode now retains Metabase after acknowledgment, legacy history retains BigQuery without reasking, and a malformed marker no longer revives NetSuite. No unresolved finding remains from this review.

Verified pending Plan Mode state, preservation of clarification-card contents, and valid Drive state. Read the updated streaming/nonstreaming selection paths and per-invocation reset. The lead’s focused regression log `/tmp/analytics-source-routing-review-fixes.log` reports **261 passed, 2 warnings**; the six Plan Mode cases cover pending/Metabase/Drive across both entry paths. This follow-up was limited to the three fixes; no deployment or broader repeated review.

## Original findings (resolved)

1. **Resolved P1 — Plan Mode selections do not replace the active request context.** `backend/app/services/chat/agents/unified_agent.py:979-981` and `:1050-1052` skip routing for both clarify-only and resumed source-choice turns, returning an empty `SourceSelection`. Neither turn persists the new pending/selected analysis state. Reproduction: old persisted analytics source NetSuite → new chosen Metabase clarification card → resumed Metabase answer → “Thanks” → “Break those results down by channel.” The acknowledgment copies the old NetSuite context (`source_selection.py:120-122`), and the next resolver cuts history at that marker (`:136-148`), selecting NetSuite instead of the user's chosen Metabase. Persist a pending analytics context for a fresh card and the server-validated selected context for its resume, preserving the card dictionary. Plan Mode also supports `drive`, which the new `RequestContext.sources` enum currently excludes.

2. **Resolved P2 — The first acknowledgment in a legacy session erases the remembered source.** `backend/app/services/chat/source_selection.py:120-127`. When history has an explicit active-analysis source choice but no persisted request context yet, a conversation continuation writes a new empty conversation context. That prevents the next analytics continuation from taking the legacy-history fallback and hides all earlier user choices. Reproduction: legacy “Use BigQuery to count orders for August” + answer → “Thanks” → “Break those orders down by status.” The resolver asks BigQuery or NetSuite again. Preserve the absence of a marker for an acknowledgment with no prior context, or safely migrate the active legacy analysis before creating one.

3. **Resolved P3 — Invalid latest context falls back to older source choices.** `backend/app/services/chat/source_selection.py:132-134`. `previous_request_context` intentionally returns `None` for an invalid latest marker instead of reviving older state, but the caller treats that identically to legacy history and scans all prior user messages. An old “Use NetSuite,” followed by a malformed newer marker, still selects NetSuite on an analytics continuation. Distinguish a missing marker from an invalid marker and avoid legacy fallback for the latter. No user-controlled metadata path was found; this is state robustness, not a demonstrated authorization bypass.

## Validation and scope

- Read streaming/nonstreaming integration, request classification, source selection, history assembly/persistence, Plan Mode filtering/card handling, tool inventory/prompt guidance, and relevant adapter implementation.
- Ran three deterministic Python reproductions through the real request-context/source-selection helpers with the existing backend virtualenv. All reproduced the findings above.
- The ordinary analytics gate carries selected context and token counts in both entry paths. Persistence merges into existing structured output, and existing tool authorization, tenant scope, mutation interception, and exact-change approval checks remain in their existing execution paths.
- Did not duplicate the lead's focused/broader tests or live classification probes. No additional issue confirmed in the reviewed guard or adapter paths.
