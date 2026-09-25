# Native reconciliation batches

Bulk SuiteQL identity and refund-neighborhood reads cover up to ten orders. Native REST commercial fields and the existing deterministic refund verifier remain authoritative. Immutable tenant-scoped PostgreSQL batches survive worker continuation. There is no new public API, infrastructure, financial write, tax profile or model configuration.

## Evidence contract

Read-only Framework REST-role probes showed why custom SuiteQL totals cannot replace native amounts: in one AU order, `custbody_stc_tax_after_discount` was 24.91 while native `taxTotal` was 27.64 (shipping tax). Native transaction-tax rows also exist for legacy-tax orders without a REST SuiteTax sublist. Assembly components appear in SuiteQL but not the native item sublist. These are account/role-scoped observations.

All-date identity queries preserve duplicate and cross-subsidiary matches. Complete graph neighborhoods retain touching outside-order edges. The existing verifier still checks currency, ownership, refund applications, credit/deposit distinctions and optional configured tax-ledger proof. Custom reverse ownership is requeried. An unqueried graph node is never treated as empty.

## Storage and recovery

Migration 113 adds immutable batches with a tenant/run composite FK, forced RLS, config/phase/parent digest, credential fingerprint and original collection interval. Decimals remain exact strings. Identical observation replays produce the same ID and cannot refresh timestamps. A batch alone does not establish scan or daily coverage.

The runner reserves 35 calls per batch: up to 32 actual native calls plus the existing OAuth allowance. Existing metering settles actual sends; individual fallback requires a separate reservation. Batches are scoped to the current scan cycle and are reusable for at most ten minutes, below the existing fifteen-minute financial freshness gate. Configuration, phase, parent-batch and credential changes fence durable reuse. Fresh financial-write preflights remain unchanged.

This is an operational first bulk path, not an assertion that all native evidence can be extracted via SuiteQL. Commercial records are still retrieved once individually within the shared batch. New daily observations still need change validation; completed review coverage uses the existing daily-evidence mechanism.

## Validation

Initial integrated focused suite: 218 passed, including seeded reconciliation lifecycle. Additional replay/runner tests pass. Local migration tested in isolated localhost database `recon_bulk_staging_test`; no orphan migration applied to staging.

Live read-only Inc sample: five order projections and refund results matched the individual path exactly, excluding observation times and call accounting. Calls 37 → 11; elapsed collection 24.312s → 8.838s. All five had zero refunds. This small sequential sample is not an end-to-end throughput benchmark; provider warming and order mix may affect timing.

Private artifacts: `/Users/aidenyi/.codex/artifacts/recon-bulk-staging-20260924`. Full CI, independent pre-merge review, staging migration/deployment and live smoke are required release gates.

Independent-review follow-ups also preserve individual-read budget headroom and reject prefetched orders older than the source's updated_at, or refunds older than a newly read source refund observation. Cached evidence is an as-observed comparison, not a claim of current provider state. Financial decisions still require existing fresh preflights. The full schema check also exposed a pre-existing EvalScoreHistory model-only mismatch: its inherited updated_at was never created by migration057; the model now maps only the existing created_at.

Three positive-refund cases also matched exactly (each verified refund100.00): native calls25→14, collection13.017s→10.352s. Post-review focused tests:143 passed. Full schema validation passes after the model correction. Collectors have a90-second batch timeout that falls back under a new reservation, while the runner's deadline remains authoritative.
