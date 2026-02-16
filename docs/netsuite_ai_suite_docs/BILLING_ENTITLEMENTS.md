# Billing & Entitlements
_Last updated: 2026-02-15_

## Principles
- Feature gating is server-side.
- Every gated attempt is logged.
- Writeback is behind: paid entitlement + tenant enablement + per-action approval.

## Trial entitlements (2 months)
- Connect NetSuite + 1 external source
- Table visibility (read-only)
- Limited SuiteQL tool calls
- CSV exports limited
- Reconciliation limited; evidence packs disabled
- Scheduling disabled

## Pro entitlements
- Higher/unlimited SuiteQL tool calls (policy-configurable)
- Excel/HTML exports
- Evidence packs enabled
- Scheduling enabled
- Admin Copilot + Change Requests enabled
- Optional writeback feature flag (requires approvals)

## Metering candidates
- SuiteQL tool calls
- Reconciliation runs
- Scheduled jobs executed
- Payout lines processed
- Evidence pack exports
