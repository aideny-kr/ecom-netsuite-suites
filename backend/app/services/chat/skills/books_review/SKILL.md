---
Name: Books Review / GL Hygiene
Description: Read-only bookkeeping review — inspects the general ledger for hygiene issues (suspense/clearing balances, unreconciled accounts, miscoding, cutoff gaps) and recommends adjusting entries. Never posts.
Triggers:
  - /books-review
  - books review
  - gl hygiene
  - clean up the books
---

# Books Review / GL Hygiene

You are executing the Books Review / GL Hygiene skill. This is **read-only and advisory**: you inspect the ledger and *recommend* corrections — you do NOT and cannot post, adjust, or modify any entry. Posting is a separate, human-approved step.

This skill is source-agnostic: review whatever general ledger is reachable through your tools. Today that is the NetSuite ledger; the same method applies to any ledger (for example QuickBooks) once its tools are connected.

1. **Scope.**
   - Confirm the period under review (default: the current open period). Ask if the user wants a full review or a specific area (e.g. just suspense accounts).

2. **Inspect the ledger — never invent balances.**
   - Use `netsuite_financial_report` and schema-discovered `netsuite_suiteql` (discover fields first; do not assume them) to examine:
     - **Suspense / clearing / ask-my-accountant accounts** with non-zero balances that should net to zero.
     - **Unreconciled** bank and control accounts.
     - **Duplicate or likely-missing entries** and out-of-balance subledgers vs the GL control account.
     - **Coding inconsistencies** — amounts posted to unexpected or inactive accounts, or miscategorized expense / revenue.
     - **Cutoff / accrual gaps** — expense or revenue in the wrong period, missing accruals or prepaids.
     - **Impossible balances** — for example negative inventory value, or a debit balance in a liability account.

3. **Recommend fixes.**
   - Produce a prioritized findings list. For each finding, describe the issue, its likely cause, and the **recommended adjusting entry or reclassification** in plain terms (which accounts, direction, and why). Make clear each is a recommendation for a human to review and post.

4. **Summarize.**
   - Lead with the count and severity of issues and the books' overall readiness. End with the entries that must be cleared before close.

## Output discipline
The figures are rendered automatically by the tool — lead with the insight and the "so what". Do NOT restate, re-list, or recompute the numbers in prose. Present recommended entries as guidance only; never claim to have posted anything.
