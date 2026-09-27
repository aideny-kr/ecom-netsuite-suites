# Scheduled execution recovery

Scheduled jobs use the existing approved plan registry and jobs/audit tables.
No new transaction or backfill executor is enabled by this change. Those surfaces
remain separately gated by their supported executor, evidence and approval contracts.

## Durable boundaries

- Enqueue/claim saves the plan, version, principal and budget before dispatch. A
  pending row is an outbox entry; the minute sweep recovers entries older than one
  minute. A future retry remains dormant until its original due time.
- A dedicated PostgreSQL transaction advisory lock serializes each tenant/schedule
  across all execution commits. Another worker leaves pending work for recovery.
  Process death releases the lock. Its dedicated connection holds one transaction
  throughout execution, so executor commits and transaction poolers cannot move it.
- An exact existing job must belong to the tenant, schedule and job type. Missing
  or mismatched identifiers fail closed; they never create replacement operations.
  Terminal broker redelivery returns recorded results without executing steps.
- Each run records an operation ID; its bounded retry retains that ID, original
  plan/period/principal and remaining budget. Plan-version drift blocks a retry.
  Existing report delivery retains its schedule/report-step/period identity.
- Pause, disable, cancellation and version changes are checked between steps;
  wall-time budget also bounds an awaited executor. A blocking synchronous call
  cannot be forcefully interrupted by asyncio; its uncertain result is fenced.
  In-flight remote requests cannot be recalled by pausing a schedule.
- Write intent and paid-model reservations are committed before outbound effects.
  Non-success after such intent is `reason=blocked, verification=uncertain`, with
  the schedule paused. No automatic retry or new run-now request can bypass that unresolved operation;
  resume returns 409 with its job ID.
- The complete execution receipt is committed before final bookkeeping. A worker
  killed after this receipt can settle the job without repeating any call. A
  running job without this receipt and with effect intent is uncertain. Interrupted
  reads without durable write/model intent fail without replay; future independent
  occurrences remain usable.
  Generic application-startup stale-job cleanup excludes scheduled jobs so it
  cannot erase their recovery state or race a live worker.

## Recovery limits and operator handoff

An unknown remote outcome is **not verified success** and absence of a local
receipt is **not evidence that nothing happened**. Inspect the exact job,
operation/period identity, step-intent audit, source report and remote destination.
Existing Drive delivery locates files by identity and updates existing files;
existing transaction-operation recovery remains the authority for its own writes.
Do not merge the unrelated open PR218/195 branches to obtain a new write surface.

Operators with current company `schedules.manage` permission can call
`POST /api/v1/schedules/{schedule_id}/runs/{job_id}/reconcile`. This bounded,
read-only recovery operation supports **fully delivered scheduled Drive reports**:

1. Before any Drive call, the scheduler durably binds the job/step/report/period,
   connector identity and credential revision to the SHA-256 hashes, lengths and
   types of the actual rendered PDF and workbook. A unique attempt marker is
   written atomically with the bytes on both creates and updates, so even
   byte-identical older files cannot prove this attempt completed. Successful prior steps also
   receive durable receipts. Legacy attempts without these bindings stay uncertain.
2. The reconciler uses that same active company connector to locate unique files
   within the original folders and compare provider content checksums, size, type
   and identity. Duplicate files, incomplete searches, absent checksums, partial
   delivery, changed credentials/destination and unavailable evidence fail closed.
   A matching name or an old same-period file alone is insufficient.
3. Every started effect and every plan step must be accounted for. A report match
   cannot clear an unknown paid-model reservation, another provider's operation,
   or an unexecuted later step. Missing remote files do not authorize a resend.
4. Verified settlement records `jobs.run.reconciled` atomically with the original
   run's completion and recovered output links. It sends no delivery, invokes no
   model and replays no step. Repeating reconciliation or broker delivery is safe.
   Cancellation is preserved. Evidence failures and authorization revoked during read-back are audited;
   database failures may prevent an audit write. All leave the run fenced.
5. The schedule remains paused. Review its current plan and use the existing
   `/resume` endpoint explicitly to permit future approved occurrences; other
   uncertain runs still block resume. Execution identity and consumed budgets are
   retained, never reset. Reconciliation has its own 20-second read-back deadline.

This scheduler deliberately provides **no generic “clear uncertain and retry”
endpoint**. Partial or unsupported outcomes require provider-specific evidence and
remain stopped. Do not manually redeliver an uncertain scheduled period: it can
replace the bound content and prevent reconciliation. A double finalization
failure may lack step receipts and also remains stopped. Do not edit database
state to force a replay. Future backfill and
financial executors must supply their own supported recovery contract before they
can enter this registry; existing transaction-operation recovery remains separate.
The verification is an observation of exact content at read-back time, not a
promise that an external Drive editor will never change it later.

Drive evidence follows the official [file metadata](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)
and [list completeness](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list)
contracts. These checks use synthetic providers in tests; no live Google delivery
or production rollout is claimed by the integration checks.

At upgrade, legacy pending rows older than one minute with no durable dispatch
snapshot are retired as blocked without execution and audited. A later broker
delivery returns that terminal result. Inspect these retired occurrences before
resuming a schedule whose previous state is unclear.

Tests use real PostgreSQL sessions, synthetic external effects and separate
process SIGKILL before/after the completion receipt, after both synthetic Drive
uploads, and before reconciliation's settlement commit. Concurrent operators and
broker delivery share the schedule execution lock. No live ERP writes or paid
provider calls are needed. This change is integration-only until the separate
backup/cutover and deployment gates pass.
