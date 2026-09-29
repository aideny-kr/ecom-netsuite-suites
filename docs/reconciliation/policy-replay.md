# Historical refund-policy equivalence pilot

A refund-reason-list revision previously required a fresh historical investigation even when the rule could not affect most orders. The explicit policy-replay API now evaluates saved observations without provider or model calls. It is a separate historical receipt, not a current reconciliation run, daily coverage receipt, case update, approval or posting authorization.

The pilot supports only a direct configuration successor where the symmetric difference of `refund_adjustments.tax_reversal_reason_ids` is the sole mapping change. Source, destination, account, subsidiary, record type, extraction and evidence contracts must match. Adding and removing reason IDs both matter. A previously matched order can become affected. Every unsupported delta is refused.

## Use

With the normal authenticated API and `recon.run` permission:

1. `POST /api/v1/transaction-ops/configs/{successor_config_id}/policy-replays` with `evaluation_key` (UUID), `start_date` and inclusive `end_date`. Dates use the configuration's timezone and review basis; at most 31 completed calendar days.
2. `GET /api/v1/transaction-ops/policy-replays/{id}` returns candidate/processed counts and the three outcomes. `finished` means all pinned candidates were evaluated, **not** that a period is financially complete.
3. `GET /api/v1/transaction-ops/policy-replays/{id}/entries?outcome=affected` (or `unknown`/`equivalent`) returns original finding/run references, SHA-256 fingerprints, original read times, evaluation time and reasons. Pagination uses the last `order_reference` as `after`, at most 500 rows per page.
4. `POST /api/v1/transaction-ops/policy-replays/{id}/resume` republishes pending work after a broker failure. `POST .../cancel` stops further batches and preserves completed receipts. A cancelled receipt is terminal; use a new key for another evaluation.

The create response's `queued` field reports publication, not completion. Transient worker failures allow up to three automatic retries. Permission, contract and missing-source errors (403/404/409) stop immediately with an actionable `last_error_code`; correct the cause before resuming, or cancel. Each batch rechecks the initiating user's current permission. `updated_at` exposes the last durable progress; stale pending work can be resumed or cancelled. Contract changes and database timeouts have explicit error codes. Retrying the same key and dates returns the same receipt. Reusing a key with different dates/configuration is rejected. Only one pending replay per successor is permitted.

## Meaning and limitations

- **Equivalent:** complete saved refund evidence contains no changed reason; the original deterministic balance takes the same policy branches. Money and original read times are retained. JEV judgments are not copied.
- **Affected:** a changed reason occurs in the saved request links/proof. Current collector behavior is non-additive, so these require targeted evidence collection; the pilot does not reconstruct discarded ledger data.
- **Unknown:** incomplete, malformed, size-limited, changed or incompatible evidence. Absence is never treated as zero.

The pinned population is the latest retained finding per order in the predecessor's original scan windows, including matched orders and continuation slices. Original completed-scan coverage is disclosed independently. Findings alone cannot prove that no order was omitted during discovery or persistence. Therefore `population_completeness` remains `unverified`, even when old scan windows cover the whole date range. This pilot does **not** automatically skip or replace an active custom review, or certify the whole month. That requires a complete retained discovery/population manifest and a separate current-state refresh contract.

No hidden cache-miss refresh occurs. Affected/unknown references can be fed to the existing explicit exact-order investigation path (maximum 200 per run). Such a run creates **current** evidence; it cannot repair an unavailable historical snapshot. The pilot is available through the API; it does not change the existing custom-period UI.

## Execution and release

Creation deduplicates identities before hashing large payloads and pins at most 50,000 identities/fingerprints under one transaction; an over-limit request fails without a partial receipt. Existing control workers read/evaluate 200 entries per atomic result/progress commit, below scheduler priority. Each batch has a 20-second total timeout; a task yields after a 25-second loop budget (50-second soft / 55-second hard task limits). This avoids waiting behind bulk provider scans without adding infrastructure. Duplicate workers serialize on the replay, and a failed batch rolls back completely. Pinning and evaluation have bounded database statements. Source evidence changing after pinning becomes unknown. Receipt evidence and manifests have database update guards and tenant RLS/composite foreign keys. Existing runs, cases and financial operations are never updated.

Migration `116_policy_replays` adds two isolated tables; no historical backfill or provider call. Rollback the image to disable the API/task, leaving receipts intact. Drain replay tasks before any schema downgrade. Keep every backend/worker/beat service on the same image. No frontend rebuild or infrastructure change is needed.

Before release: focused equivalence/provider-fixture, database/API/RLS/recovery tests; independent review of the exact revision; required CI and reconciliation lifecycle e2e. After release: isolated live UAT, authenticated receipt pilot, and measured DB/evaluation/persistence time plus equivalent/affected/unknown counts. Report observed throughput separately from full current-state reconciliation capacity; no claimed provider-call savings without a comparable baseline.
