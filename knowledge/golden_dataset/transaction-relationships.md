---
topic: transaction_relationships
source_type: curated
priority: high
---

# Transaction Relationships in NetSuite

## The `createdfrom` Field

The `createdfrom` field on both `transaction` (header level) and `transactionline`
is the primary mechanism for tracing transaction chains in NetSuite.

### Common Transaction Chains

| Source Transaction | Created Transaction | Relationship |
|-------------------|-------------------|-------------|
| Sales Order (SalesOrd) | Invoice (CustInvc) | SO fulfillment → billing |
| Sales Order (SalesOrd) | Item Fulfillment (ItemShip) | SO → shipping |
| Purchase Order (PurchOrd) | Item Receipt (ItemRcpt) | PO → receiving |
| Purchase Order (PurchOrd) | Vendor Bill (VendBill) | PO → billing |
| Return Authorization (RtnAuth) | Item Receipt (ItemRcpt) | RMA → return receiving |
| Return Authorization (RtnAuth) | Credit Memo (CustCred) | RMA → customer credit |
| Invoice (CustInvc) | Customer Payment (CustPymt) | Invoice → payment |
| Invoice (CustInvc) | Credit Memo (CustCred) | Invoice → credit |
| Vendor Bill (VendBill) | Vendor Payment (VendPymt) | Bill → payment |
| Transfer Order (TrnfrOrd) | Item Receipt (ItemRcpt) | Transfer → receiving |
| Work Order (WorkOrd) | Assembly Build (Build) | WO → build completion |

### Query Patterns for Finding Related Transactions

**Find Item Receipts created from a specific RMA:**
```sql
SELECT t.id, t.tranid, t.trandate, BUILTIN.DF(t.status) as status,
       BUILTIN.DF(t.entity) as entity_name
FROM transaction t
WHERE t.type = 'ItemRcpt'
  AND t.createdfrom = <rma_internal_id>
```

**Find all transactions in a chain (SO → Invoice → Payment):**
```sql
-- Start from Sales Order, find all downstream transactions
SELECT child.id, child.tranid, child.type, child.trandate,
       BUILTIN.DF(child.status) as status
FROM transaction child
WHERE child.createdfrom = <sales_order_id>
ORDER BY child.trandate
```

**Find the source transaction for an Item Receipt:**
```sql
SELECT parent.id, parent.tranid, parent.type, parent.trandate,
       BUILTIN.DF(parent.status) as status
FROM transaction t
JOIN transaction parent ON parent.id = t.createdfrom
WHERE t.id = <item_receipt_id>
```

### RMA-Specific Patterns

**RMA Status Codes (via REST API — single letter):**
| Code | Meaning |
|------|---------|
| A | Pending Approval |
| B | Pending Receipt |
| C | Cancelled |
| D | Partially Received |
| E | Pending Refund |
| F | Refunded |
| H | Closed |

**Find open RMAs with pending receipts:**
```sql
SELECT t.id, t.tranid, t.trandate, t.status,
       BUILTIN.DF(t.entity) as customer,
       BUILTIN.DF(t.status) as status_display
FROM transaction t
WHERE t.type = 'RtnAuth'
  AND t.status NOT IN ('C', 'H', 'F')
ORDER BY t.trandate DESC
```

**Find RMA line items with quantities:**
```sql
SELECT t.tranid as rma_number,
       BUILTIN.DF(tl.item) as item_name,
       tl.quantity as return_qty,
       tl.rate,
       tl.amount
FROM transaction t
JOIN transactionline tl ON tl.transaction = t.id
WHERE t.type = 'RtnAuth'
  AND t.id = <rma_id>
  AND tl.mainline = 'F'
```
