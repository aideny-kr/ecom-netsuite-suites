# Changelog

## 0.1.0.0 — 2026-09-05

### Added

- Framework transaction investigations from the UI, chat and bounded schedules,
  using tenant-scoped Celigo source connections and NetSuite reads. Runs retain
  progress, detailed findings, call budgets and explicit termination reasons.
- Exact transaction-currency comparison for missing orders, amount and VAT
  differences, with complete inventory identities and explicit legacy tax or
  finalized source-assessment policies. Unsupported or incomplete evidence
  remains for review; no statutory rate or currency conversion is inferred.
- Immutable human proposals for amount corrections, missing-order creation and
  proven Celigo duplicate-error resolution. Execution rechecks approval,
  permissions and fresh provider evidence before one committed send permit.
- An isolated NetSuite guard for existing unfulfilled orders and new orders in
  pending approval, with exact native previews, accounting-period checks,
  currency/FX preservation, inventory routing and durable order attribution.
  Both native write switches default off.
- Independent post-write verification and bounded read-only recovery. Unknown
  outcomes prevent another write; a known no-write failure permits at most one
  further attempt with a new human decision. Large creation proofs retain
  original observations and integrity-bound references within the ledger limit.
- Setup, schedule controls, run history, detailed evidence, frozen approval and
  outcome rechecks. Chat displays computed financial evidence directly.
- Local seeded HTTP/worker tests that kill an execution process after a stub
  save and prove one write, read-only recovery and exact temporary-tenant cleanup.
- Required CI jobs for the frontend unit suite and SuiteApp native guard tests.

### Delivery status

This is a draft PR delivery. Native deployment, account-specific save automation
validation and the blocking T2 review are still required before release. No live
NetSuite financial write is included in validation.
