# Observability (Logs, Metrics, Traces)
_Last updated: 2026-02-15_

## Objectives
- Fast diagnosis of connector failures and slow jobs
- Tenant-aware debugging (without leaking cross-tenant data)
- SLOs for critical workflows (sync freshness, recon completion)

## Logging
- Structured JSON logs with:
  - tenant_id, connection_id, job_id, correlation_id
  - component (api/worker/connector/mcp)
- Redact sensitive fields

## Metrics
- API: p95 latency, error rates
- Jobs: throughput, retries, failures, run duration
- Connectors: rate-limit hits, external API error codes
- Reconciliation: rows processed, mismatches found

## Tracing
- Distributed tracing across API → worker → connector calls
- Propagate correlation IDs into audit events

## Alerting & runbooks
- Alert on sustained sync failures, writeback failures, recon backlog
- Runbooks for common issues (auth expired, rate limits, permissions)
