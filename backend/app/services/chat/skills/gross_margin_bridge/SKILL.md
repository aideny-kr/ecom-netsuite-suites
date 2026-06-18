---
Name: Gross-Margin Bridge
Description: Explains why gross margin changed between two periods — reads the tool-rendered per-product revenue/quantity/cost and attributes the move directionally to price, volume, mix, and cost.
Triggers:
  - /margin-bridge
---

# Gross-Margin Bridge

You are executing the Gross-Margin Bridge skill. Read-only and advisory — you never hand-derive a waterfall of numbers. Follow these steps:

1. **Scope.**
   - Identify the two periods to bridge (current vs comparison). Default to the most recent closed month vs the prior month if unspecified; state your choice.

2. **Get the data from tools — never invent the split.**
   - Use `netsuite_suiteql` to return per-product revenue, quantity, and cost with the aggregation and per-product margin computed **in the query**, so the result is rendered as a table (discover the fields from the schema first; do not assume field names). Pull the gross-margin totals for both periods from `netsuite_financial_report` to anchor the discussion.

3. **Attribute the change directionally.**
   - From the rendered per-product data, explain which effects drove the margin change — **price** (selling price moved), **volume** (units moved), **mix** (shift toward higher/lower-margin products), **cost** (unit cost moved) — and which products dominated. Describe direction and relative magnitude from what the table shows.
   - A precise additive price-volume-mix decomposition is a multi-step computation: do NOT produce the exact per-effect dollars by hand. If the user needs an exact bridge, offer to add it as a blessed metric so it is computed and rendered.

4. **Narrate.**
   - Lead with which effect dominated and why, in business terms. End with the lever most worth acting on.

## Output discipline
The tool renders every figure automatically as a table/report — give COMMENTARY ONLY. Do NOT restate, reproduce, or recompute the numbers in prose, and never do the financial arithmetic yourself. If a figure is not returned by a tool, describe it qualitatively or offer to add it as a blessed metric — never present a self-computed number as authoritative. For multi-part output, call `report_compose` and reference each prior result by its `result_id`.
