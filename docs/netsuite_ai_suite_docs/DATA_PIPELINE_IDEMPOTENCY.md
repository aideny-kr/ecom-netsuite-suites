# Data Pipeline & Idempotency
_Last updated: 2026-02-15_

## Goals
- Incremental syncs that are retry-safe
- Deterministic dedupe across replays/backfills
- Safe writeback without duplicates

## Ingestion patterns
### Incremental cursors
Store per-connection cursor state:
- last seen timestamps
- pagination cursors / high-water marks
Support backfills with bounded date windows.

### Dedupe keys
Every ingested row has a stable natural key:
(tenant_id, source, object_type, object_id, event_version)

## Idempotent jobs
- Celery tasks are safe to retry (UPSERT by natural key)
- Record outcomes and counts for audit

## Writeback safety (paid + approvals)
- All writeback operations require an idempotency key per object/batch
- Persist payload hash, idempotency key, and NetSuite response ids
- On retry, detect prior success and return the same outcome

## Failure handling
- Exponential backoff + jitter for rate limits
- Dead-letter queue for repeated failures
- Operator dashboard for replays and investigation
