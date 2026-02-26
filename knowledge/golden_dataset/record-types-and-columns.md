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
- `netamount` — Amount after discounts
- `mainline` — 'T' for header pseudo-line, 'F' for item lines
- `taxline` — 'T' for tax lines, 'F' for non-tax
- `linesequencenumber` — Order of lines
- `class`, `department`, `location` — Line-level classifications

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
