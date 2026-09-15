# Analytics source selection

Implemented on `fix/analytics-source-routing`, based on `c1ca948e`. This candidate has not been deployed or merged.

The previous global data-source gate also intercepted transaction and integration work. Its case exception depended on exact follow-up phrases and silently treated “Solidus” as a Metabase choice.

The new flow classifies request purpose and continuity before applying source selection. A small, forced classification tool uses the configured chat adapter/model and has no data tools or execution authority. The deterministic source resolver then asks only for an unresolved analytics source. “Solidus” identifies a dataset and does not select Metabase. Explicit source choices are retained for the active analysis; new independent analyses do not inherit a source from an unrelated operation.

| Request | Behavior |
| --- | --- |
| Count Solidus orders in batch 395 | Ask which connected source to use |
| Use Metabase to count Solidus orders | Use the explicitly selected source |
| Break those orders down by status | Retain the active analysis source |
| Fix it / show me the evidence for this case | Continue the transaction workflow |
| Retry a Celigo flow | Continue the operation without analytics clarification |
| Count Solidus orders after fixing an invoice | Start a new analytics source choice |

Task context is server-persisted alongside existing `ChatMessage.structured_output`, preserving charts and signed cards. No schema migration is required. Pending/selected Plan Mode context, acknowledgments, legacy sessions, source switches/refusals, and invalid context are covered. Request context does not grant connector access or financial approval; existing execution, tenant, and exact-change approval guards remain in place.

The classifier is called only when the multi-source gate is relevant; documentation/workspace context and single-source inventory avoid that call. Classification failures stop the turn before data/operational execution, rather than querying an arbitrary source. Cancellation propagates. Routing token usage is included in normal successful-turn and clarification-turn accounting. In the live acceptance sample, classification took about 1.2–2.1 seconds per request.

## Verification

- 448 source routing, history, inventory, Plan Mode, mutation/approval, and audit tests passed. Four existing AsyncMock fixture warnings remain.
- 32 real-database tenant-isolation and Metabase OAuth/auth tests passed.
- 14 synthetic live-model routing acceptance cases passed using staging's configured `claude-sonnet-5`, in an isolated process. This exercised the candidate classifier and resolver without deploying it or invoking data tools. See [evidence](analytics-source-routing-evidence.json).
- Independent review found and then verified fixes for Plan Mode source replacement, legacy acknowledgments, and invalid-marker fallback. No unresolved finding remains; see [review](analytics-source-routing-review.md).
- Ruff lint/format and `git diff --check` passed.

This validation covers the changed routing behavior; it is not a new browser/deployed-chat acceptance run. The numerical aggregate-enforcement issue documented in the earlier Metabase review is a separate outstanding change.
