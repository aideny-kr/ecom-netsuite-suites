---
Name: AR / AP Aging Triage
Description: Buckets open receivables or payables by age and computes DSO/DPO in the query (rendered as a table), then flags concentration risk and prioritizes a collections or payment action list.
Triggers:
  - /aging
---

# AR / AP Aging Triage

You are executing the AR / AP Aging Triage skill. Read-only and advisory — you never post, write off, or apply anything, and you never sum or divide in your head. Follow these steps:

1. **Determine AR or AP.**
   - If the user said receivables / collections / "who owes us", do AR. If payables / "what we owe" / vendor bills, do AP. If ambiguous, ask once which one before proceeding.

2. **Pull the aging — let the query do the math.**
   - Aging is **not** an income-statement or balance-sheet report; if a financial-mode hint suggests those report types, ignore it here. Use `netsuite_suiteql` over the open AR/AP items and do the bucketing and the DSO/DPO **inside the query** — group by age band (0-30 / 31-60 / 61-90 / 90+), sum the balances, and compute DSO (AR) or DPO (AP) in SQL — so the result is rendered as a table. Discover the relevant fields from the schema first; do not assume field names. (If the tenant exposes a standard aging report through `netsuite_financial_report`, that rendered report is fine too.)

3. **Read risk and concentration off the rendered table.**
   - Identify the counterparties holding the largest overdue balances and any single-name concentration. For AR, flag balances aging past terms and any credit-risk signals. For AP, flag bills approaching or past due and any early-payment discounts about to lapse.

4. **Prioritize actions.**
   - For AR: a ranked collections sequence (largest / oldest first, with a suggested next step per account). For AP: a ranked payment plan that protects discounts and avoids late penalties while preserving cash.

## Output discipline
The tool renders every figure automatically as a table/report — give COMMENTARY ONLY. Do NOT restate, reproduce, or recompute the numbers in prose, and never do the financial arithmetic yourself. If a figure is not returned by a tool, describe it qualitatively or offer to add it as a blessed metric — never present a self-computed number as authoritative. For multi-part output, call `report_compose` and reference each prior result by its `result_id`.
