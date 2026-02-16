# NetSuite Bundle / SuiteApp Plan
_Last updated: 2026-02-15_

## Why bundle?
- Reduce friction: consistent installable package.
- Enable RESTlets/custom records where SuiteTalk alone is insufficient.
- Create distribution advantage via SuiteBundler.

## Bundle contents (target)
1) RESTlet(s) for optimized extraction and writeback (if needed)
2) Custom record types for integration configuration (optional)
3) Script deployments with safe defaults
4) Optional saved searches for reconciliation

## Security posture
- Dedicated integration role with least privilege
- All writes are gated by SaaS approvals + idempotency keys
