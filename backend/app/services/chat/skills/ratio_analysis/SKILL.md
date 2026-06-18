---
Name: Financial Ratio Analysis
Description: Reads the standard liquidity, leverage, profitability, and efficiency ratios — preferring blessed metrics for exact values — and interprets them against conventions and the prior period.
Triggers:
  - /ratios
---

# Financial Ratio Analysis

You are executing the Financial Ratio Analysis skill. Read-only and advisory — you never do the division yourself. Follow these steps:

1. **Scope.**
   - Confirm the period (default: most recent closed period) and whether a prior-period comparison is wanted (default: yes, vs the preceding period).

2. **Get the figures from tools — never compute a ratio in prose.**
   - For each ratio, prefer `metric_compute` if a blessed metric exists for it — it computes and renders the exact value. Pull the component figures (current assets, liabilities, equity, debt, operating income, margins, turnover inputs) from `netsuite_financial_report` (balance sheet + income statement), which renders them.
   - For a ratio with **no** blessed metric: do NOT type a computed ratio value. Read the rendered components and describe the relationship qualitatively (e.g. "current assets comfortably exceed current liabilities — healthy short-term liquidity"), and offer to add the ratio as a blessed metric so it computes deterministically.

3. **Cover the panel.**
   - **Liquidity:** current ratio, quick ratio. **Leverage:** debt-to-equity, interest coverage. **Profitability:** gross / operating / net margin, return on assets, return on equity. **Efficiency:** asset, inventory, and receivables turnover. Skip any whose inputs are unavailable and say so.

4. **Benchmark and interpret.**
   - Compare each ratio to its standard convention range (e.g. current ratio typically around 1.5–3; quick ratio at or above ~1; interest coverage comfortably above ~2–3) and to the prior period; note that healthy ranges are industry-dependent. Call out deterioration and what it implies about liquidity, solvency, and returns.

5. **Narrate.**
   - Lead with an overall health read, then group by liquidity / leverage / profitability / efficiency. End with the ratios trending the wrong way.

## Output discipline
The tool renders every figure automatically as a table/report — give COMMENTARY ONLY. Do NOT restate, reproduce, or recompute the numbers in prose, and never do the financial arithmetic yourself. If a figure is not returned by a tool, describe it qualitatively or offer to add it as a blessed metric — never present a self-computed number as authoritative. For multi-part output, call `report_compose` and reference each prior result by its `result_id`.
