---
topic_tags: ["suiteql", "schema", "record-types", "columns"]
source_type: documentation
---

# Record Types and Core Columns

## Core Tables and Their Key Columns

### transaction
The central table for all financial documents (orders, invoices, payments, etc.).

Key columns:
- `id` — Primary key (sequential, higher = newer)
- `tranid` — Human-readable transaction number (e.g., "SO865732")
- `type` — Transaction type code (e.g., 'SalesOrd', 'CustInvc')
- `trandate` — Transaction date
- `entity` — Customer/vendor ID (FK to customer/vendor)
- `status` — Compound status code (e.g., 'SalesOrd:B')
- `foreigntotal` — Total in transaction currency
- `total` — Total in subsidiary base currency
- `currency` — Currency record ID
- `exchangerate` — Exchange rate to base currency
- `subsidiary` — Subsidiary ID
- `department`, `class`, `location` — Classification segments
- `memo` — Free text memo
- `otherrefnum` — External reference number
- `createddate` — Record creation timestamp

### transactionline
Line items for transactions. Always joined to transaction via `tl.transaction = t.id`.

Key columns:
- `id` — Line ID
- `transaction` — FK to transaction.id
- `item` — FK to item.id
- `quantity` — Item quantity
- `rate` — Unit price
- `foreignamount` — Line total in transaction currency (NEGATIVE for revenue)
- `amount` — Line total in base currency (NEGATIVE for revenue)
- `netamount` — Amount after discounts (may not exist in all accounts — use `foreignamount` as fallback)
- `mainline` — 'T' for header pseudo-line, 'F' for item lines
- `taxline` — 'T' for tax lines, 'F' for non-tax
- `iscogs` — 'T' if this is a Cost of Goods Sold line, 'F' otherwise
- `expenseaccount` — GL account ID for the line
- `linesequencenumber` — Order of lines
- `class`, `department`, `location` — Line-level classifications

**COGS / Margin queries**: Do NOT use `accounttype` (it does not exist on transactionline). COGS lines are identified by `tl.iscogs = 'T'`. Important: COGS lines typically appear on **Item Fulfillment** (`ItemShip`) transactions, NOT on invoices (`CustInvc`). To get margin, query COGS and revenue from ALL posted transactions — do not filter by a single transaction type.

**CRITICAL — Currency consistency**: Always use the SAME currency column for both revenue and COGS. Use `tl.amount` (base currency, usually USD) for both. NEVER mix `tl.foreignamount` (transaction currency) with `tl.amount` (base currency) — this produces nonsense results for multi-currency accounts because `foreignamount` sums AUD + GBP + EUR as if they were the same currency.

**Sign convention**: Both revenue and COGS line amounts are NEGATIVE on their respective transactions. Revenue lines on invoices (`CustInvc`) have negative `amount`; COGS lines on fulfillments (`ItemShip`) also have negative `amount`. Always multiply by -1 to get positive values for both.

```sql
-- Revenue from invoices (base currency USD)
SELECT TO_CHAR(t.trandate, 'YYYY-MM') as month,
    SUM(tl.amount * -1) as revenue_usd
FROM transactionline tl
JOIN transaction t ON tl.transaction = t.id
WHERE t.type = 'CustInvc' AND t.posting = 'T'
  AND tl.mainline = 'F' AND tl.taxline = 'F'
  AND t.trandate >= TO_DATE('2025-01-01', 'YYYY-MM-DD')
GROUP BY TO_CHAR(t.trandate, 'YYYY-MM')
ORDER BY month

-- COGS from all posted transactions (base currency USD)
-- COGS lines live on ItemShip (fulfillments), journals, etc. — NOT on CustInvc
SELECT TO_CHAR(t.trandate, 'YYYY-MM') as month,
    SUM(tl.amount * -1) as cogs_usd
FROM transactionline tl
JOIN transaction t ON tl.transaction = t.id
WHERE tl.iscogs = 'T' AND t.posting = 'T'
  AND t.trandate >= TO_DATE('2025-01-01', 'YYYY-MM-DD')
GROUP BY TO_CHAR(t.trandate, 'YYYY-MM')
ORDER BY month
```

Margin % = (revenue_usd - cogs_usd) / revenue_usd * 100.

**Note**: The `transactionaccountingline` table (with `account.accttype = 'COGS'`) is an alternative for GL-level COGS lookup, but it may be blocked by some policy profiles. Prefer `tl.iscogs = 'T'` as the primary approach.

### customer
- `id` — Primary key
- `companyname` — Company name
- `email` — Email address
- `entityid` — Customer number/ID
- `subsidiary` — Subsidiary ID
- `isperson` — 'T' for individual, 'F' for company

### item
- `id` — Primary key
- `itemid` — Item name/number
- `displayname` — Display name
- `type` — Item type (InvtPart, NonInvtPart, Service, etc.)
- `baseprice` — Base price

### employee / vendor
Similar structure to customer with role-specific fields.

## Foreign Key Relationships

```
transaction.entity → customer.id / vendor.id
transactionline.transaction → transaction.id
transactionline.item → item.id
transaction.subsidiary → subsidiary.id
transaction.currency → currency.id
```

## Inventory Tables

NetSuite has multiple approaches for inventory data. Try them in this order:

### Approach 1: inventoryitemlocations (PREFERRED — most reliable via SuiteQL REST API)

The `inventoryitemlocations` table provides per-item, per-location stock data and is widely accessible via SuiteQL REST API.

Key columns:
- `item` — FK to item.id
- `location` — FK to location.id
- `quantityonhand` — Current on-hand quantity
- `quantityavailable` — Available quantity (on-hand minus committed)
- `quantityonorder` — Quantity on purchase orders
- `quantityintransit` — In-transit quantity
- `preferredstocklevel` — Reorder point
- `reorderpoint` — Reorder point threshold
- `quantitybackordered` — Backordered quantity
- `quantitycommitted` — Committed quantity

```sql
-- Inventory by location for specific items
SELECT iil.item, i.itemid, i.displayname,
       BUILTIN.DF(iil.location) as location_name,
       iil.quantityonhand, iil.quantityavailable,
       iil.quantityonorder, iil.quantitycommitted
FROM inventoryitemlocations iil
JOIN item i ON iil.item = i.id
WHERE LOWER(i.itemid) LIKE '%ddr5%'
  AND iil.quantityonhand > 0
ORDER BY i.itemid, location_name

-- Total stock across all locations
SELECT i.itemid, i.displayname,
       SUM(iil.quantityonhand) as total_onhand,
       SUM(iil.quantityavailable) as total_available
FROM inventoryitemlocations iil
JOIN item i ON iil.item = i.id
WHERE i.type = 'InvtPart'
GROUP BY i.itemid, i.displayname
HAVING SUM(iil.quantityonhand) > 0
ORDER BY total_onhand DESC
FETCH FIRST 50 ROWS ONLY

-- Items below reorder point
SELECT i.itemid, i.displayname,
       BUILTIN.DF(iil.location) as location_name,
       iil.quantityavailable, iil.reorderpoint
FROM inventoryitemlocations iil
JOIN item i ON iil.item = i.id
WHERE iil.reorderpoint IS NOT NULL
  AND iil.quantityavailable < iil.reorderpoint
ORDER BY i.itemid
FETCH FIRST 50 ROWS ONLY
```

### Approach 2: inventorybalance (FALLBACK — may be restricted in some accounts)

**WARNING**: `inventorybalance` is often restricted via SuiteQL REST API and may return "Invalid search" errors. If it fails, switch to `inventoryitemlocations` above.

Key columns: `item`, `location`, `quantityonhand`, `quantityavailable`, `quantityonorder`, `quantityintransit`, `status`.

```sql
SELECT ib.item, i.itemid, i.displayname,
       BUILTIN.DF(ib.location) as location_name,
       ib.quantityonhand, ib.quantityavailable
FROM inventorybalance ib
JOIN item i ON ib.item = i.id
WHERE LOWER(i.itemid) LIKE '%ddr5%'
ORDER BY location_name
```

### Approach 3: item table quantity fields (SIMPLEST — no joins needed)

The `item` table itself has aggregate quantity fields. These show totals across ALL locations.

```sql
SELECT id, itemid, displayname,
       totalquantityonhand, quantityavailable, quantityonorder,
       quantitycommitted, quantitybackordered
FROM item
WHERE type = 'InvtPart'
  AND LOWER(itemid) LIKE '%ddr5%'
ORDER BY itemid
FETCH FIRST 50 ROWS ONLY
```

**Note**: These fields (`totalquantityonhand`, `quantityavailable`) may not be available on all accounts. If they return "Unknown identifier", fall back to `inventoryitemlocations`.

### inventorynumber

Serial and lot number tracking for inventory items.

Key columns:
- `id` — Primary key
- `item` — FK to item.id
- `inventorynumber` — The serial/lot number string
- `expirationdate` — Expiration date (for lot-tracked items)

### Inventory item filtering

To find only inventory-tracked items, filter the `item` table:
```sql
SELECT id, itemid, displayname, baseprice
FROM item
WHERE type = 'InvtPart'
  AND isinactive = 'F'
ORDER BY itemid
FETCH FIRST 50 ROWS ONLY
```

Item types: `InvtPart` (inventory), `NonInvtPart` (non-inventory), `Service`, `Kit`, `Assembly`, `Group`.

## Custom Record Tables

Custom records use the naming convention `customrecord_<scriptid>` (always lowercase in SuiteQL):
```sql
-- Discover columns
SELECT * FROM customrecord_r_inv_processor WHERE ROWNUM <= 5

-- Query with filters
SELECT id, name, custrecord_field1
FROM customrecord_r_inv_processor
WHERE isinactive = 'F'
```
