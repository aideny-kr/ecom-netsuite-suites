# Playbooks (v1)
_Last updated: 2026-02-15_

## Playbook 1 — Daily Payout Variance Monitor
Steps:
1) Sync last 7 days (incremental)
2) Reconcile payout totals
3) Flag variances above threshold
4) Notify (email; Slack add-on later)
Outputs:
- Variance table + evidence pack per payout (paid)

## Playbook 2 — Month-End Close Evidence Pack
Steps:
1) Run reconciliation for full month
2) Produce rollup summary (matched/unmatched/aged)
3) Export evidence pack bundle
Outputs:
- xlsx exports + audit summary

## Playbook 3 — Chargeback & Dispute Workflow
Steps:
1) Sync disputes/chargebacks
2) Match to orders and NetSuite entries
3) Flag missing accounting entries or timing mismatches
Outputs:
- Table view + recommended next steps

## Playbook — Subsidiary-Aware Balance Check
Goal:
- Show configured balance sheet amounts for cash/clearing/refund accounts (per subsidiary and consolidated where feasible) and explain differences vs reconciliation totals.

Steps:
1) Pull NetSuite balances for configured accounts per subsidiary.
2) Compare to computed totals from ingested payout/refund events.
3) Drill down discrepancies to the specific payouts/lines/orders/refunds that contribute.

## Playbook — Scheduled Reports
Goal:
- Let users schedule saved views and copilot-generated reports (email delivery).

Implementation notes:
- Scheduler executes report build → export → email send.
- Every run produces an audit event and artifact references.
