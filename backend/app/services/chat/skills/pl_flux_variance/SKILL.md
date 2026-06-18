---
Name: P&L Flux / Variance Analysis
Description: Explains period-over-period income-statement movements — the tool renders the line-level variances; you flag the material movers and attribute each to a volume, price, mix, or timing/accrual driver.
Triggers:
  - /flux
---

# P&L Flux / Variance Analysis

You are executing the P&L Flux / Variance Analysis skill. This is read-only and advisory — you never post or modify anything, and you never do the arithmetic yourself. Follow these steps:

1. **Scope the comparison.**
   - Identify the two periods to compare. If the user named them (e.g. "May vs April", "Q2 vs Q1", "this year vs last"), use those. If only one period is given, compare it to the immediately preceding period of equal length. If none is given, default to the most recent closed month vs the prior month, and state which periods you used.
   - Honor the tenant fiscal calendar already in your context for quarter/year boundaries.

2. **Let the tool compute and render the variances.**
   - Call `netsuite_financial_report` with a comparative or trend income statement (e.g. an income-statement-trend covering both periods) so the **tool** computes the per-line period columns and the dollar/percent variances and renders them in a table. Do not write a query for a standard statement, and do not subtract the periods yourself.

3. **Decide what is material.**
   - From the rendered variances, focus on the lines that moved materially — a threshold the user stated, otherwise lines moving roughly 5%+ of the prior-period value. The table already shows the amounts; your job is to say which ones matter, not to re-type them.

4. **Attribute the driver.**
   - For each material variance, name the most likely driver class and explain it in business terms: **volume** (units/activity changed), **price/rate** (selling price or cost rate changed), **mix** (composition shifted between higher/lower-margin items), **timing/accrual** (a cutoff, accrual, or deferral effect rather than real economic change), or **one-off** (a non-recurring item). Flag where a movement looks like a reclassification or data anomaly rather than performance.

5. **Narrate.**
   - Lead with the headline (margin expanded or compressed, and what drove it). Group the explanation by driver. End with the one or two items the controller should investigate.

## Output discipline
The tool renders every figure automatically as a table/report — give COMMENTARY ONLY. Do NOT restate, reproduce, or recompute the numbers in prose, and never do the financial arithmetic yourself. If a figure is not returned by a tool, describe it qualitatively or offer to add it as a blessed metric — never present a self-computed number as authoritative. For multi-part output, call `report_compose` and reference each prior result by its `result_id`.
