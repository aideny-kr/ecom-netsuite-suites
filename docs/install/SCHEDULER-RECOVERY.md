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

This scheduler deliberately provides **no generic “clear uncertain and retry”
endpoint**. Provider-specific reconciliation must prove the exact outcome before
that operation can be retried or marked verified. Generic automatic reconciliation
and a user-facing resolution flow are not implemented here. Until a supported
reconciler records that proof, leave the job uncertain and the schedule stopped;
do not edit database state to force a replay. Thus this is a safe recovery
foundation, not complete FW-013 acceptance for every future effect type.

Tests use real PostgreSQL sessions, synthetic external effects and separate
process SIGKILL before/after the completion receipt. No live ERP writes or paid
provider calls are needed. This change is integration-only until the separate
backup/cutover and deployment gates pass.
