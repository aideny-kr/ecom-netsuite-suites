---
Name: Month-End Close Checklist
Description: Guides the controller through the standard month-end close sequence — subledger recon, GL hygiene, accruals/cutoff, flux review, balance-sheet recon, and a period-close readiness summary. Advisory only; never performs the lock.
Triggers:
  - /close-checklist
---

# Month-End Close Checklist

You are executing the Month-End Close Checklist skill. Read-only and advisory — you assess readiness and recommend; you never post entries or lock the period. Walk the controller through this sequence, reporting status for each step and what remains. Fetch any figures via the existing report/query tools (which render them); never total or compute numbers yourself.

1. **Subledger reconciliation.**
   - Confirm AR, AP, bank, and any settlement subledgers tie to the GL control accounts. Use the product's reconciliation engine for Stripe / deposit matching where applicable. Flag unreconciled differences.

2. **Books / GL hygiene** (the `/books-review` method).
   - Clear suspense and clearing accounts, fix miscoding, and resolve impossible balances. List recommended adjusting entries for a human to post.

3. **Accruals, prepaids, deferrals, depreciation; cutoff.**
   - Verify recurring accruals and prepaid amortization are booked, revenue / expense cutoff is correct, and depreciation has run. Flag anything missing as a recommended entry.

4. **Intercompany / eliminations** (if applicable).
   - Confirm intercompany balances net and eliminations are booked.

5. **P&L flux review** (the `/flux` method).
   - Run a variance review of the income statement vs the prior period; investigate material, unexplained movements before close.

6. **Balance-sheet reconciliation.**
   - Confirm each material balance-sheet account is supported by a reconciliation or schedule.

7. **Ratio sanity check** (the `/ratios` method).
   - Sanity-check key ratios for anomalies that suggest a posting error.

8. **Readiness summary.**
   - Summarize what is done, what is blocking close, and the recommended entries outstanding. State clearly that locking the period is a human action taken outside this advisory skill.

## Output discipline
The tool renders every figure automatically as a table/report — give COMMENTARY ONLY. Do NOT restate, reproduce, or recompute the numbers in prose, and never do the financial arithmetic yourself. Present the checklist status and recommended entries as guidance; never claim to have posted or locked anything.
