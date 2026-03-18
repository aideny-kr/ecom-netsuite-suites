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

**CRITICAL — REST API vs Local SuiteQL difference:**
- In the **local SuiteQL editor**, `t.status` in WHERE clauses matches compound codes like `'SalesOrd:B'`.
- Via the **REST API**, `t.status` returns **single-letter codes** (e.g., `'B'`, `'C'`, `'H'`), so compound codes will NOT match and filters silently fail.
- **Always use single-letter codes** in status filters: `t.status NOT IN ('C', 'H')` — NOT `t.status NOT IN ('SalesOrd:C', 'SalesOrd:H')`.
- Use `BUILTIN.DF(t.status)` for readable names in SELECT.

```sql
SELECT t.tranid,
       t.status as raw_status,
       BUILTIN.DF(t.status) as status_name
FROM transaction t
WHERE t.type = 'SalesOrd'
FETCH FIRST 5 ROWS ONLY
```

## DISCOVERY APPROACH — PREFERRED OVER HARDCODED STATUSES

Status codes can vary by tenant configuration (approval routing, custom workflows).
The most reliable approach is to discover statuses dynamically:

```sql
-- Run this once per tenant at onboarding to discover all status codes
SELECT DISTINCT t.type, t.status, BUILTIN.DF(t.status) AS status_name
FROM transaction t
ORDER BY t.type, t.status
```

The statuses below are from a real NetSuite instance (discovered 2026-03-18).
Use them as DEFAULTS but always prefer tenant-discovered values when available.

## Sales Order Statuses — `type = 'SalesOrd'`

- `A` → Pending Approval
- `B` → Pending Fulfillment
- `C` → Cancelled
- `E` → Pending Billing/Partially Fulfilled
- `F` → Pending Billing
- `G` → Billed
- `H` → Closed

```sql
-- Open sales orders (not cancelled, not closed)
WHERE t.type = 'SalesOrd' AND t.status NOT IN ('C', 'H')

-- Pending fulfillment
WHERE t.type = 'SalesOrd' AND t.status = 'B'
```

## Return Authorization (RMA) Statuses — `type = 'RtnAuth'`

- `B` → Pending Receipt  ← RMA approved, waiting for goods to arrive
- `D` → Partially Received  ← some lines received
- `E` → Pending Refund/Partially Received  ← received, refund not yet issued
- `F` → Pending Refund  ← fully received, refund pending
- `G` → Refunded  ← received and refund processed
- `H` → Closed  ← fully resolved

**"Received" RMAs = status IN ('D', 'E', 'F', 'G', 'H')** — all of these imply goods were received. Only `B` (Pending Receipt) means not yet received.

```sql
-- Received RMAs (all statuses past Pending Receipt)
WHERE t.type = 'RtnAuth' AND t.status IN ('D', 'E', 'F', 'G', 'H')

-- RMAs still waiting for receipt
WHERE t.type = 'RtnAuth' AND t.status = 'B'
```

RMA → Item Receipt join (use LEFT JOIN — not all received RMAs have separate item receipt records):
```sql
-- Find item receipts linked to RMAs (for received date)
LEFT JOIN transaction ir ON ir.createdfrom = rma.id AND ir.type = 'ItemRcpt'
LEFT JOIN transactionline irl ON irl.transaction = ir.id AND irl.mainline = 'F' AND irl.taxline = 'F'
```

Only join item receipts when the user explicitly asks for **received date** or **receipt details**.
If the user just asks for "received RMAs", use the status filter — no join needed.

## Customer Invoice Statuses — `type = 'CustInvc'`

- `A` → Open  ← unpaid, due
- `B` → Paid In Full
- `V` → Voided

```sql
-- Unpaid invoices
WHERE t.type = 'CustInvc' AND t.status = 'A'
```

## Item Receipt Statuses — `type = 'ItemRcpt'`

- `Y` → Undefined (only status — item receipts exist or don't)

Item Receipts do not have meaningful statuses. Filter by existence, not status.

```sql
-- All item receipts for a PO
SELECT ir.tranid, ir.trandate, BUILTIN.DF(irl.item), ABS(irl.quantity)
FROM transaction ir
JOIN transactionline irl ON irl.transaction = ir.id AND irl.mainline = 'F' AND irl.taxline = 'F'
WHERE ir.type = 'ItemRcpt' AND ir.createdfrom = :po_id
```

## Vendor Bill Statuses — `type = 'VendBill'`

- `A` → Open  ← unpaid
- `B` → Paid In Full

```sql
-- Unpaid vendor bills
WHERE t.type = 'VendBill' AND t.status = 'A'
```

## Purchase Order Statuses — `type = 'PurchOrd'`

- `A` → Pending Supervisor Approval
- `B` → Pending Receipt  ← ordered, not yet received
- `D` → Partially Received
- `E` → Pending Billing/Partially Received
- `F` → Pending Bill  ← received, not yet billed
- `G` → Fully Billed
- `H` → Closed

```sql
-- Open POs (not fully received)
WHERE t.type = 'PurchOrd' AND t.status IN ('B', 'D', 'E')
```

## Transfer Order Statuses — `type = 'TrnfrOrd'`

- `B` → Pending Fulfillment
- `F` → Pending Receipt
- `G` → Received
- `H` → Closed

## Item Fulfillment Statuses — `type = 'ItemShip'`

- `A` → Picked
- `C` → Shipped

## Credit Memo Statuses — `type = 'CustCred'`

- `B` → Fully Applied
- `V` → Voided

## Customer Payment Statuses — `type = 'CustPymt'`

- `B` → Not Deposited
- `C` → Deposited

## Vendor Payment Statuses — `type = 'VendPymt'`

- `V` → Voided
- `Y` → Undefined

## Journal Entry Statuses — `type = 'Journal'`

- `B` → Approved for Posting
- `V` → Voided

## Work Order Statuses — `type = 'WorkOrd'`

- `B` → Released
- `G` → Built
- `H` → Closed

## Estimate/Quote Statuses — `type = 'Estimate'`

- `B` → Processed

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
