# Reconciliation Spec (v1)
_Last updated: 2026-02-15_

## Objective
Detect and explain discrepancies between:
- Shopify/Stripe operational & settlement data (orders, refunds, payouts, fees)
and
- NetSuite financial postings (deposits, payments, journal entries, cash sales)

## Canonical model
Define canonical events:
- order_event, payment_event, refund_event
- payout_event, fee_event, adjustment_event
- dispute_event

Each event includes:
- source ids and timestamps
- gross/fees/net
- currency + FX details (if available)
- order/customer references

## Matching strategy
### Phase 1: Deterministic
- Exact matches on payout identifiers (when available)
- Summation matches within timing window (T+0..T+3)
- Currency-consistent comparisons

### Phase 2: Rule-based fuzzy
- Tolerances for rounding/FX
- Partial capture / split payouts
- Refunds after payout

## Variance taxonomy (tag every finding)
- Fees, FX/rounding, timing, missing, duplicates, chargebacks, manual adjustments

## Explainability & evidence
For every discrepancy:
- rule fired and parameters
- source lines used
- computed totals
- recommended next action
- evidence pointers to table rows

## AI assistance (v1)
Advisory only:
- cluster similar mismatches
- suggest likely causes and next steps
Never auto-post financial entries.

## Outputs
- Table views: discrepancies, matched sets, unmatched sets
- Evidence pack export:
  - xlsx/csv of findings
  - json of run config, rule versions, and audit summary

## Operational requirements (worth it)
- Deterministic dedupe keys for ingested source objects
- Reproducible reconciliation runs (store run config + rule versions + snapshot refs)
- Findings linkable to audit events and table views

## Subsidiary-aware balance checks
For configured “cash/clearing/refund” accounts, the app should show:
- per-subsidiary balances (and consolidated where feasible)
- reconciliation totals vs balance sheet amounts
- drilldown to contributing records

## Posting preferences (paid, later phase)
Customers choose how discrepancies are posted:
- **Lumpsum**: summary journal entries per payout/period
- **Detail**: line-level journal entries including id and order reference

Operational constraints:
- batch journal creation (default ≤200 lines per JE batch)
- generate an evidence CSV per batch and attach it to the JE
