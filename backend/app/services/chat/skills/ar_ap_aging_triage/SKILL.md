---
Name: AR / AP Aging Triage
Description: Buckets open receivables or payables by age (0-30/31-60/61-90/90+), computes DSO or DPO, flags concentration risk, and produces a prioritized collections or payment action list.
Triggers:
  - /aging
  - ar aging
  - ap aging
  - aging triage
---

# AR / AP Aging Triage

You are executing the AR / AP Aging Triage skill. Read-only and advisory — you never post, write off, or apply anything. Follow these steps:

1. **Determine AR or AP.**
   - If the user said receivables / collections / "who owes us", do AR. If payables / "what we owe" / vendor bills, do AP. If ambiguous, ask once which one before proceeding.

2. **Fetch the open items — never invent them.**
   - Prefer the standard aging report via `netsuite_financial_report` (an AR or AP aging report) if available. Otherwise use `netsuite_suiteql` over open transactions: first discover the relevant columns from the schema (open balance, due date, counterparty), then query — do not assume column names.

3. **Bucket and measure.**
   - Bucket each open item by days past due into 0-30, 31-60, 61-90, and 90+. Compute the total per bucket and each bucket's share of the total balance.
   - Compute **DSO** for AR (or **DPO** for AP) for the period, and compare to the prior period if available.

4. **Find risk and concentration.**
   - Identify the counterparties holding the largest overdue balances and any single-name concentration. For AR, flag balances aging past terms and any credit-risk signals. For AP, flag bills approaching or past due and any early-payment discounts about to lapse.

5. **Prioritize actions.**
   - For AR: a ranked collections sequence (largest / oldest first, with a suggested next step per account). For AP: a ranked payment plan that protects discounts and avoids late penalties while preserving cash.

## Output discipline
The figures are rendered automatically by the tool — lead with the insight and the "so what". Do NOT restate, re-list, or recompute the numbers in prose. For a multi-part narrative, call `report_compose` and reference each prior result by its `result_id`.
