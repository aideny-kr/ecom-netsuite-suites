---
Name: P&L Flux / Variance Analysis
Description: Explains period-over-period income-statement movements — line-level $ and % variances above materiality, attributed to volume, price, mix, or timing/accrual drivers.
Triggers:
  - /flux
  - flux analysis
  - p&l variance
  - income statement variance
---

# P&L Flux / Variance Analysis

You are executing the P&L Flux / Variance Analysis skill. This is read-only and advisory — you never post or modify anything. Follow these steps:

1. **Scope the comparison.**
   - Identify the two periods to compare. If the user named them (e.g. "May vs April", "Q2 vs Q1", "this year vs last"), use those. If only one period is given, compare it to the immediately preceding period of equal length. If none is given, default to the most recent closed month vs the prior month, and state which periods you used.
   - Honor the tenant fiscal calendar already in your context for quarter/year boundaries.

2. **Fetch the figures — never invent them.**
   - Call `netsuite_financial_report` for the income statement for each period (or one comparative report if the tool supports two periods). Do not write a query for standard statements; use the report tool, and discover its available parameters rather than assuming them.

3. **Compute the flux.**
   - For each income-statement line, compute the dollar change (current − prior) and the percent change. Decide what to discuss with a materiality threshold: the greater of any threshold the user stated and **5% of the prior-period line value**. State the threshold you applied.
   - Rank lines by absolute dollar impact and focus commentary on the material movers.

4. **Attribute the driver.**
   - For each material variance, name the most likely driver class and explain it in business terms: **volume** (units/activity changed), **price/rate** (selling price or cost rate changed), **mix** (composition shifted between higher/lower-margin items), **timing/accrual** (a cutoff, accrual, or deferral effect rather than real economic change), or **one-off** (a non-recurring item). Flag where a movement looks like a reclassification or data anomaly rather than performance.

5. **Narrate.**
   - Lead with the headline (margin expanded or compressed, and what drove it). Group the explanation by driver. End with the one or two items the controller should investigate.

## Output discipline
The figures are rendered automatically by the tool — lead with the insight and the "so what". Do NOT restate, re-list, or recompute the numbers in prose. For a multi-part narrative, call `report_compose` and reference each prior result by its `result_id`.
