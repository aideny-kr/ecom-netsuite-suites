# Scheduling (Cron Jobs)
_Last updated: 2026-02-16_

## Scope
Schedule:
- reconciliation digests (daily/weekly/monthly)
- saved view exports (CSV/Excel)
- copilot-generated reports (HTML/Excel)

## Implementation (recommended)
- Celery Beat (scheduler) + Celery workers (execution)
- Store schedules in DB (tenant-scoped)

## Governance
- Scheduling is a paid entitlement.
- Every run logs an audit event (schedule_id, parameters, artifacts, delivery status).
