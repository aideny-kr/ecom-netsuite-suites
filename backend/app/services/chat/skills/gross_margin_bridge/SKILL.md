---
Name: Gross-Margin Bridge
Description: Decomposes the change in gross margin between two periods into price, volume, mix, and cost effects (PVM bridge) from line-level revenue, quantity, and cost.
Triggers:
  - /margin-bridge
  - gross margin bridge
  - margin bridge
  - price volume mix
---

# Gross-Margin Bridge

You are executing the Gross-Margin Bridge skill. Read-only and advisory. Follow these steps:

1. **Scope.**
   - Identify the two periods to bridge (current vs comparison). Default to the most recent closed month vs the prior month if unspecified; state your choice.

2. **Confirm the data is available — never invent it.**
   - A margin bridge needs line-level **revenue**, **quantity/units**, and **unit cost**. Discover the schema with `netsuite_suiteql` first to find those fields for this tenant — do not assume field names. Also pull the gross-margin totals for both periods via `netsuite_financial_report` so you can reconcile the bridge to the reported change.
   - If line-level quantity or cost is not available, say so and fall back to a top-level gross-margin variance (revenue effect vs cost effect only); do not fabricate a full price-volume-mix split.

3. **Build the bridge.**
   - Decompose the margin change into: **price** (selling-price change at constant volume/mix), **volume** (units change at constant price/mix), **mix** (shift between higher- and lower-margin products), and **cost** (unit-cost change). Present it as an additive waterfall from prior-period margin to current-period margin.
   - Reconcile: the four effects must sum to the reported margin change; flag any unexplained residual.

4. **Narrate.**
   - Lead with which effect dominated and why, in business terms. End with the lever most worth acting on.

## Output discipline
The figures are rendered automatically by the tool — lead with the insight and the "so what". Do NOT restate, re-list, or recompute the numbers in prose. For a multi-part narrative, call `report_compose` and reference each prior result by its `result_id`.
