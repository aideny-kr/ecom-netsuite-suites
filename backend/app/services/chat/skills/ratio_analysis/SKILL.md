---
Name: Financial Ratio Analysis
Description: Computes the standard liquidity, leverage, profitability, and efficiency ratio panel from the balance sheet and income statement, benchmarked against conventions and the prior period.
Triggers:
  - /ratios
  - ratio analysis
  - financial ratios
---

# Financial Ratio Analysis

You are executing the Financial Ratio Analysis skill. Read-only and advisory. Follow these steps:

1. **Scope.**
   - Confirm the period (default: most recent closed period) and whether a prior-period comparison is wanted (default: yes, vs the preceding period).

2. **Fetch the inputs — never invent them.**
   - Call `netsuite_financial_report` for the balance sheet and the income statement for the period(s). If a ratio already exists as a blessed metric, prefer `metric_compute` for it and present the blessed value rather than recomputing.

3. **Compute the panel.**
   - **Liquidity:** current ratio (current assets ÷ current liabilities), quick ratio (excluding inventory).
   - **Leverage:** debt-to-equity, interest coverage (operating income ÷ interest expense).
   - **Profitability:** gross margin, operating margin, net margin, return on assets, return on equity.
   - **Efficiency:** asset turnover, inventory turnover, receivables turnover.
   - Skip any ratio whose inputs are unavailable and say so; never fabricate a denominator.

4. **Benchmark and interpret.**
   - Compare each ratio to its standard convention range (e.g. current ratio typically around 1.5–3; quick ratio at or above ~1; interest coverage comfortably above ~2–3) and to the prior period. Note that healthy ranges are industry-dependent.
   - Call out deterioration, unusual values, and what they imply about liquidity, solvency, and returns.

5. **Narrate.**
   - Lead with an overall health read, then group by liquidity / leverage / profitability / efficiency. End with the ratios trending the wrong way.

## Output discipline
The figures are rendered automatically by the tool — lead with the insight and the "so what". Do NOT restate, re-list, or recompute the numbers in prose. For a multi-part narrative, call `report_compose` and reference each prior result by its `result_id`.
