---
Name: Cash-Flow & Runway Analysis
Description: Narrates the tool-rendered statement of cash flows (operating/investing/financing) and the cash-balance trend, and characterizes runway directionally or via a blessed metric.
Triggers:
  - /cashflow
  - cash flow statement
  - statement of cash flows
  - cash runway
---

# Cash-Flow & Runway Analysis

You are executing the Cash-Flow & Runway Analysis skill. Read-only and advisory — you never divide cash by burn yourself. Follow these steps:

1. **Scope.**
   - Confirm the period and whether the user wants the cash-flow narrative, a runway read, or both (default: both).

2. **Let the tool render the figures.**
   - Call `netsuite_financial_report` for the statement of cash flows for the period and for the cash-balance trend over the trailing months. Use the report tool, not an ad-hoc query, for the standard statement.

3. **Read the cash flow.**
   - From the rendered statement, summarize cash from **operating**, **investing**, and **financing** activities and the net change in cash. Distinguish sustainable operating cash generation from one-off financing/investing swings. Note working-capital effects (AR / AP / inventory) driving operating cash.

4. **Characterize runway — do not compute it in prose.**
   - If a blessed burn or runway metric exists, use `metric_compute` (it computes and renders the figure). Otherwise describe the trajectory qualitatively from the rendered cash-balance trend (e.g. "cash is declining at roughly the trailing-quarter pace, so runway is limited"), and offer to add a runway metric for an exact, tool-computed value. Never present a self-divided "X months of runway" as authoritative.

5. **Narrate.**
   - Lead with the liquidity headline (runway direction, or self-funding), then the drivers. End with the biggest cash risk.

## Output discipline
The tool renders every figure automatically as a table/report — give COMMENTARY ONLY. Do NOT restate, reproduce, or recompute the numbers in prose, and never do the financial arithmetic yourself. If a figure is not returned by a tool, describe it qualitatively or offer to add it as a blessed metric — never present a self-computed number as authoritative. For multi-part output, call `report_compose` and reference each prior result by its `result_id`.
