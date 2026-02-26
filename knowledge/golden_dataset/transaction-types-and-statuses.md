---
topic_tags: ["suiteql", "transactions", "types", "statuses"]
source_type: documentation
---

# Transaction Types and Statuses

## Transaction Type Codes

NetSuite uses internal type codes in the `type` column. The most common:

| Code | Transaction Type | Prefix |
|------|-----------------|--------|
| `SalesOrd` | Sales Order | SO |
| `CustInvc` | Customer Invoice | INV |
| `CustCred` | Credit Memo | CM |
| `CustPymt` | Customer Payment | PYMT |
| `RtnAuth` | Return Authorization (RMA) | RMA |
| `PurchOrd` | Purchase Order | PO |
| `VendBill` | Vendor Bill | VB |
| `VendPymt` | Vendor Payment | VPYMT |
| `TrnfrOrd` | Transfer Order | TO |
| `ItemShip` | Item Fulfillment | IF |
| `ItemRcpt` | Item Receipt | IR |
| `WorkOrd` | Work Order | WO |
| `CashSale` | Cash Sale | CS |
| `Journal` | Journal Entry | JE |
| `Estimate` | Quote/Estimate | EST |
| `Opprtnty` | Opportunity | OPP |

## Filtering by Type

```sql
-- Sales orders only
WHERE t.type = 'SalesOrd'

-- Multiple types
WHERE t.type IN ('SalesOrd', 'CashSale')

-- Get display name of type
SELECT BUILTIN.DF(t.type) as transaction_type
```

## Transaction Number (tranid) Conventions

NetSuite `tranid` typically includes the type prefix:
- "SO865732" → Sales Order
- "RMA61214" → Return Authorization
- "PO12345" → Purchase Order
- "INV98765" → Invoice

When searching by transaction number:
```sql
-- Exact match (user provides full number)
WHERE t.tranid = 'RMA61214'

-- Partial match
WHERE t.tranid LIKE '%61214%'

-- With type filter for speed
WHERE t.tranid = 'RMA61214' AND t.type = 'RtnAuth'
```

## Status Format

Transaction statuses use a compound format: `TypeCode:StatusLetter`. Use `BUILTIN.DF(t.status)` for readable names:

```sql
SELECT t.tranid,
       t.status as raw_status,
       BUILTIN.DF(t.status) as status_name
FROM transaction t
WHERE t.type = 'SalesOrd'
FETCH FIRST 5 ROWS ONLY
```

Common sales order statuses:
- `SalesOrd:A` → Pending Approval
- `SalesOrd:B` → Pending Fulfillment
- `SalesOrd:C` → Partially Fulfilled
- `SalesOrd:D` → Pending Billing/Partially Fulfilled
- `SalesOrd:E` → Pending Billing
- `SalesOrd:F` → Billed
- `SalesOrd:G` → Closed
- `SalesOrd:H` → Cancelled

## Common Date-Based Queries

```sql
-- Today's sales orders
SELECT t.id, t.tranid, BUILTIN.DF(t.entity) as customer, t.foreigntotal
FROM transaction t
WHERE t.type = 'SalesOrd' AND t.trandate = TRUNC(SYSDATE)
ORDER BY t.id DESC
FETCH FIRST 50 ROWS ONLY

-- This month's invoices
SELECT COUNT(*) as count, SUM(t.foreigntotal) as total
FROM transaction t
WHERE t.type = 'CustInvc'
  AND t.trandate >= TRUNC(SYSDATE, 'MM')
```
