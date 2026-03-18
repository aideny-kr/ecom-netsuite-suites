---
topic_tags: ["suiteql", "examples", "lookup", "aggregation", "inventory", "yoy"]
source_type: expert_rules
---

# SuiteQL Example Query Patterns

Proven SuiteQL query patterns for common NetSuite operations. Each pattern follows all SuiteQL dialect rules (FETCH FIRST, single-letter status codes, BUILTIN.DF, etc.).

## Find transaction by tranid

Look up any transaction by its document number (SO, PO, RMA, INV, etc.). The tranid field contains the full prefix+number.

```sql
SELECT t.id, t.tranid, t.trandate,
       BUILTIN.DF(t.entity) as customer,
       BUILTIN.DF(t.status) as status,
       t.foreigntotal
FROM transaction t
WHERE t.tranid = 'RMA61214'
```

## Find transaction by internal ID

Direct lookup when you have the NetSuite internal ID.

```sql
SELECT t.id, t.tranid, t.trandate,
       BUILTIN.DF(t.entity) as entity_name,
       BUILTIN.DF(t.status) as status,
       t.type, t.foreigntotal
FROM transaction t
WHERE t.id = 12345
```

## Latest N orders

Retrieve the most recent sales orders. Uses ORDER BY t.id DESC (higher id = more recent) with FETCH FIRST for pagination.

```sql
SELECT t.id, t.tranid, t.trandate,
       BUILTIN.DF(t.entity) as customer,
       BUILTIN.DF(t.status) as status,
       t.foreigntotal
FROM transaction t
WHERE t.type = 'SalesOrd'
ORDER BY t.id DESC
FETCH FIRST 10 ROWS ONLY
```

## Find customer by name

Case-insensitive name search using LOWER + LIKE. Keep columns minimal — companyname, email, id are universally safe.

```sql
SELECT id, companyname, email
FROM customer
WHERE LOWER(companyname) LIKE '%acme%'
```

## Sales by currency (today)

Header-level aggregation grouped by currency. Uses BUILTIN.DF to resolve currency ID to display name.

```sql
SELECT BUILTIN.DF(t.currency) as currency,
       COUNT(*) as order_count,
       SUM(t.foreigntotal) as total
FROM transaction t
WHERE t.type = 'SalesOrd'
  AND t.trandate = TRUNC(SYSDATE)
GROUP BY BUILTIN.DF(t.currency)
ORDER BY total DESC
```

## Sales by class — year-over-year comparison

Line-level YoY analysis with 2-3 GROUP BY dimensions max. Uses tl.amount * -1 for positive revenue, CASE for fiscal year bucketing, and all required line filters (mainline, taxline, assemblycomponent).

```sql
SELECT CASE WHEN t.trandate >= TO_DATE('2026-01-01','YYYY-MM-DD')
            THEN 'FY2026' ELSE 'FY2025' END as fiscal_year,
       BUILTIN.DF(i.class) as product_class,
       COUNT(DISTINCT t.id) as order_count,
       ROUND(SUM(tl.amount * -1), 2) as revenue_usd
FROM transactionline tl
  JOIN transaction t ON tl.transaction = t.id
  JOIN item i ON tl.item = i.id
WHERE t.type = 'SalesOrd'
  AND tl.mainline = 'F' AND tl.taxline = 'F'
  AND tl.assemblycomponent = 'F'
  AND ((t.trandate >= TO_DATE('2025-01-01','YYYY-MM-DD')
        AND t.trandate <= TO_DATE('2025-12-31','YYYY-MM-DD'))
    OR (t.trandate >= TO_DATE('2026-01-01','YYYY-MM-DD')
        AND t.trandate <= TO_DATE('2026-12-31','YYYY-MM-DD')))
GROUP BY CASE WHEN t.trandate >= TO_DATE('2026-01-01','YYYY-MM-DD')
              THEN 'FY2026' ELSE 'FY2025' END,
         BUILTIN.DF(i.class)
ORDER BY fiscal_year DESC, revenue_usd DESC
```

## Inventory by item at all locations

Uses inventoryitemlocations (NOT inventorybalance). Join item table for item details. Filter quantityavailable > 0 to exclude empty locations (retry without filter if 0 rows).

```sql
SELECT i.itemid, i.displayname,
       BUILTIN.DF(iil.location) as location,
       iil.quantityavailable,
       iil.quantityonhand
FROM inventoryitemlocations iil
  JOIN item i ON i.id = iil.item
WHERE LOWER(i.itemid) LIKE '%frafmk0006%'
  AND iil.quantityavailable > 0
ORDER BY i.itemid
FETCH FIRST 100 ROWS ONLY
```

## Revenue by platform (custom field aggregation)

Groups by a custom body field using BUILTIN.DF for display values. Check tenant_vernacular for the correct custbody_ field name.

```sql
SELECT BUILTIN.DF(t.custbody_platform) as platform,
       COUNT(*) as order_count,
       SUM(t.foreigntotal) as total
FROM transaction t
WHERE t.type = 'SalesOrd'
  AND t.trandate >= TRUNC(SYSDATE) - 7
GROUP BY BUILTIN.DF(t.custbody_platform)
ORDER BY total DESC
```

## Open purchase orders

Uses single-letter status codes. PurchOrd statuses: G=Fully Billed, H=Closed. Exclude both for "open" POs.

```sql
SELECT t.id, t.tranid, t.trandate,
       BUILTIN.DF(t.entity) as vendor,
       BUILTIN.DF(t.status) as status,
       t.foreigntotal
FROM transaction t
WHERE t.type = 'PurchOrd'
  AND t.status NOT IN ('G', 'H')
ORDER BY t.id DESC
FETCH FIRST 50 ROWS ONLY
```

## Received RMAs (simple status filter)

RMAs with items received — use status codes D/E/F/G, NOT an ItemRcpt join. G=Refunded confirms receipt. Status already tells you whether items were received.

```sql
SELECT t.tranid, t.trandate,
       BUILTIN.DF(t.entity) as customer,
       BUILTIN.DF(t.status) as status,
       t.foreigntotal
FROM transaction t
WHERE t.type = 'RtnAuth'
  AND t.status IN ('D', 'E', 'F', 'G', 'H')
ORDER BY t.trandate DESC
FETCH FIRST 50 ROWS ONLY
```

## Received RMAs at a specific location

Filter by location on TRANSACTIONLINE (not transaction header). `t.location` is often empty — always use `tl.location` for location filtering.

```sql
SELECT t.tranid, t.trandate,
       BUILTIN.DF(t.entity) as customer,
       BUILTIN.DF(t.status) as status,
       loc.name as location,
       t.foreigntotal
FROM transaction t
  JOIN transactionline tl ON tl.transaction = t.id
    AND tl.mainline = 'F' AND tl.taxline = 'F'
  JOIN location loc ON loc.id = tl.location
WHERE t.type = 'RtnAuth'
  AND t.status IN ('D', 'E', 'F', 'G', 'H')
  AND UPPER(loc.name) LIKE '%PANURGY%'
  AND t.trandate >= TO_DATE('2026-02-01', 'YYYY-MM-DD')
  AND t.trandate <= TO_DATE('2026-02-28', 'YYYY-MM-DD')
ORDER BY t.trandate DESC
FETCH FIRST 50 ROWS ONLY
```

## Line-level revenue with assembly component filter

For line-level breakdown, use tl.amount * -1 (base currency, negated for positive revenue). Always filter mainline='F', taxline='F', assemblycomponent='F' to avoid double-counting assembly kit components.

```sql
SELECT BUILTIN.DF(i.displayname) as item,
       SUM(tl.amount * -1) as revenue_usd
FROM transactionline tl
  JOIN transaction t ON tl.transaction = t.id
  JOIN item i ON tl.item = i.id
WHERE t.type = 'SalesOrd'
  AND tl.mainline = 'F'
  AND tl.taxline = 'F'
  AND tl.assemblycomponent = 'F'
GROUP BY BUILTIN.DF(i.displayname)
ORDER BY revenue_usd DESC
FETCH FIRST 50 ROWS ONLY
```
