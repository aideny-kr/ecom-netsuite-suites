# Moat Strategy
_Last updated: 2026-02-16_

## Moat thesis
This product is defensible because it combines three hard-to-copy capabilities into a single workflow for NetSuite ecommerce finance:

1) **Visibility moat (table-first)**
- A unified table model across sources (Shopify/Stripe) and NetSuite, with drilldowns from payout → lines → adjustments → postings → discrepancies.

2) **Trust moat (finance-grade)**
- Reproducible reconciliation, evidence packs, immutable audit logs, and approval-gated writeback.
- “Retry-safe and never double-post” is part of the product, not a backend detail.

3) **Context moat (account-aware + subsidiary-aware)**
- Onboarding captures account-specific policies and mappings so the copilot and reconciliation run in the customer’s accounting reality (subsidiaries, clearing accounts, refund accounts, posting preferences).

## What we must productize (not optional)
### A) Evidence packs as the default output
Every reconciliation run produces:
- discrepancy tables + raw source lines
- rule fired + parameters + versions
- totals and variance
- approvals (if writeback attempted)
- export artifacts (CSV/Excel + JSON metadata)

### B) Subsidiary-aware balance checks
The system must be able to show (per subsidiary and consolidated where possible):
- balance sheet amounts for configured “cash / clearing / refund” accounts
- reconciliation totals vs balance sheet totals
- variance drilldown and where it comes from

### C) Posting preferences as a customer-controlled policy
Customers choose how discrepancies are posted:
- **Lumpsum** entries (summary)
- **Detailed** entries with id and order reference
For detailed posting:
- batch journal creation (e.g., ≤200 lines per JE batch)
- attach evidence CSV to the Journal Entry for audit support

### D) Scheduled automation (paid)
- Scheduled emails and reports (“cron jobs”) based on saved views or reconciliation results.

## Wedge messaging
- “Stop trusting black-box sync statuses. See every payout line, every posting, and every discrepancy.”
- “Close the books faster with evidence packs and auditable approvals.”
- “Account-aware and subsidiary-aware: built for real NetSuite environments.”
