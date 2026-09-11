---
Name: Sales by Platform Analysis
Description: Breaks down order sales by product platform with distinct order counts and explicitly scoped currencies.
Triggers:
  - /sales-by-platform
  - sales by platform
  - platform breakdown
  - revenue by platform
---

# Sales by Platform Analysis

1. **Establish the measure and scope:**
   - Respect the user's selected data source and use its available tools and SQL dialect. These instructions do not select NetSuite over Metabase or another source.
   - Use the requested dates. If dates are omitted, state the current-month assumption; use a half-open date range and the user's timezone.
   - Distinguish operational order sales from accounting revenue. Never label sales-order totals as recognized revenue. For a named financial metric, use its metric catalog definition; for GL revenue, establish posting accounts, book, accounting periods and reporting currency.

2. **Discover the platform and amount fields:**
   - Use verified tenant field mappings and current metadata. Framework's `i.custitem_fw_platform` is a scoped field hint, not a universal item field.
   - Verify the amount field's currency basis and sign convention. Keep transaction currency, subsidiary base currency and consolidated currency distinct. Group separate currencies unless an authorized reporting basis supplies the conversion.
   - For NetSuite order sales, use SalesOrd item lines, the tenant's verified line exclusions (including tax, COGS and assembly components), and the appropriate platform mapping. Use the selected native MCP or scoped local query tool.
   - Count distinct orders at the requested grain. Do not sum repeated header totals after a line join. Preserve credits/adjustments instead of applying ABS() to make every amount positive.

3. **Validate and present:**
   - Aggregate in the source query and label the table with Platform, Distinct Orders, Order Sales and Currency (or the user's verified financial measure).
   - A multi-platform order can appear in more than one platform count. Compute the overall distinct-order total separately; do not sum platform counts.
   - Add monetary totals only within a common verified currency/reporting basis. Rank platforms within that basis and state any missing mappings or partial coverage.
