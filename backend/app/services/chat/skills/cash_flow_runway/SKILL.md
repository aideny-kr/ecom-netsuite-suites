---
Name: Cash-Flow & Runway Analysis
Description: Narrates the statement of cash flows (operating/investing/financing) and estimates runway from the cash-balance trend and net monthly burn.
Triggers:
  - /cashflow
  - cash flow analysis
  - cash runway
  - runway analysis
---

# Cash-Flow & Runway Analysis

You are executing the Cash-Flow & Runway Analysis skill. Read-only and advisory. Follow these steps:

1. **Scope.**
   - Confirm the period and whether the user wants the cash-flow narrative, a runway estimate, or both (default: both).

2. **Fetch the figures — never invent them.**
   - Call `netsuite_financial_report` for the statement of cash flows for the period and for the cash-balance trend over the trailing months. Use the report tool, not an ad-hoc query, for the standard statement.

3. **Read the cash flow.**
   - Summarize cash from **operating**, **investing**, and **financing** activities and the net change in cash. Distinguish sustainable operating cash generation from one-off financing/investing swings. Note working-capital effects (AR / AP / inventory) driving operating cash.

4. **Estimate runway.**
   - From the trailing cash-balance trend, estimate the average net monthly burn (or build). If burning, compute runway = current cash ÷ average monthly burn, and state the assumption window. If cash-flow positive, say so and frame the build instead. Never present a runway figure without the burn assumption behind it.

5. **Narrate.**
   - Lead with the liquidity headline (runway in months, or self-funding), then the drivers. End with the biggest cash risk.

## Output discipline
The figures are rendered automatically by the tool — lead with the insight and the "so what". Do NOT restate, re-list, or recompute the numbers in prose. For a multi-part narrative, call `report_compose` and reference each prior result by its `result_id`.
