---
name: netsuite-reconciliation
description: >
  NetSuite ecommerce reconciliation — matching Shopify/Stripe payouts against NetSuite
  transactions, identifying discrepancies, and generating reconciliation reports. Use this
  skill whenever the user mentions reconciliation, recon, payout matching, payment matching,
  transaction matching, discrepancy detection, missing payments, unmatched orders, settlement
  reconciliation, bank reconciliation, revenue reconciliation, or any comparison between
  ecommerce platform data and NetSuite financial records. Also trigger when the user asks
  about payout discrepancies, financial close processes, month-end reconciliation, or wants
  to build/extend the reconciliation system.
---

# NetSuite Reconciliation

You are an expert in ecommerce-to-NetSuite financial reconciliation. You understand the full
payment lifecycle from customer checkout through payment processor settlement to NetSuite
journal entries, and you can identify discrepancies at every stage.

## Architecture Overview

The reconciliation system compares data from three sources:

```
Source of Truth Stack:

1. Ecommerce Platform (Shopify)     → Orders, line items, tax, shipping
2. Payment Processor (Stripe/etc.)  → Charges, refunds, payouts, fees
3. NetSuite                         → Sales orders, invoices, payments, deposits

Reconciliation matches across these three layers to find:
- Missing records (order exists in Shopify but not NetSuite)
- Amount mismatches (Shopify total ≠ NetSuite total)
- Extra records (NetSuite record without matching ecommerce order)
- Timing differences (payout date vs. deposit date)
- Fee discrepancies (processor fees not properly recorded)
```

### Key Backend Files

| File | Purpose |
|------|---------|
| `backend/app/mcp/tools/recon_run.py` | Reconciliation execution tool (MCP) |
| `backend/app/services/ingestion/shopify_sync.py` | Shopify data source |
| `backend/app/mcp/tools/netsuite_suiteql.py` | NetSuite data queries |
| `backend/app/workers/tasks/` | Celery tasks on `recon` queue |

## Reconciliation Tool

The `recon.run` MCP tool is the entry point for reconciliation:

**Parameters:**
- `date_from` (YYYY-MM-DD) — Start of reconciliation window
- `date_to` (YYYY-MM-DD) — End of reconciliation window
- `payout_ids` (array, optional) — Specific payouts to reconcile

**Expected Output:**
```json
{
    "run_id": "uuid",
    "status": "completed",
    "findings_count": 5,
    "summary": {
        "orders_matched": 142,
        "orders_with_discrepancies": 3,
        "missing_in_netsuite": 1,
        "missing_in_ecommerce": 1,
        "total_discrepancy_amount": 45.23
    },
    "discrepancies": [
        {
            "type": "amount_mismatch",
            "shopify_order": "#1234",
            "netsuite_tranid": "SO-5678",
            "shopify_amount": 150.00,
            "netsuite_amount": 145.50,
            "difference": 4.50,
            "likely_cause": "Shipping not mapped"
        }
    ]
}
```

The tool currently returns a stub. Here's the implementation pattern to follow when building it out.

## Reconciliation Logic

### Step 1: Gather Source Data

**From Shopify (canonical tables):**
```sql
-- Orders in the reconciliation window
SELECT external_id, order_number, total_price, subtotal_price,
       total_tax, total_shipping, total_discounts, currency,
       created_at, financial_status
FROM canonical_orders
WHERE tenant_id = :tenant_id
  AND created_at BETWEEN :date_from AND :date_to
```

**From NetSuite (via SuiteQL):**
```sql
SELECT t.id, t.tranid, t.type, t.trandate, t.foreigntotal,
       t.currency, t.externalid, t.status
FROM transaction t
WHERE t.type IN ('SalesOrd', 'CustInvc', 'CustPymt')
  AND t.trandate BETWEEN TO_DATE(:date_from, 'YYYY-MM-DD')
                      AND TO_DATE(:date_to, 'YYYY-MM-DD')
  AND ROWNUM <= 1000
```

### Step 2: Build Match Keys

Match records across systems using these keys (in priority order):

1. **External ID match** — `shopify:{order_id}` ↔ NetSuite `externalid`
2. **Order number match** — Shopify `#1234` ↔ NetSuite `otherrefnum`
3. **Amount + date fuzzy match** — Same total, same date ±1 day (last resort)

### Step 3: Compare Matched Records

For each matched pair, verify:

| Check | Shopify Field | NetSuite Field | Tolerance |
|-------|--------------|---------------|-----------|
| Total amount | `total_price` | `foreigntotal` | $0.01 (rounding) |
| Currency | `currency` | `currency` | Exact match |
| Line items count | `line_items.length` | Line count on SO | Exact |
| Tax amount | `total_tax` | Tax total on lines | $0.05 |
| Shipping | `total_shipping` | Shipping line amount | $0.01 |
| Customer | `customer.email` | Customer email on SO | Case-insensitive |
| Date | `created_at` | `trandate` | ±1 day |

### Step 4: Classify Discrepancies

| Type | Description | Severity |
|------|-------------|----------|
| `missing_in_netsuite` | Shopify order with no NetSuite match | High |
| `missing_in_ecommerce` | NetSuite record with no Shopify match | Medium |
| `amount_mismatch` | Totals don't match within tolerance | High |
| `tax_mismatch` | Tax amounts differ | Medium |
| `shipping_mismatch` | Shipping amounts differ | Low |
| `line_count_mismatch` | Different number of line items | Medium |
| `currency_mismatch` | Currency codes differ | High |
| `date_mismatch` | Dates differ by more than 1 day | Low |
| `status_mismatch` | Shopify fulfilled but NS not invoiced | Medium |

### Step 5: Generate Findings

Each discrepancy gets:
- A unique finding ID
- Source records from both systems
- Calculated difference amount
- Likely cause (based on pattern matching)
- Suggested resolution

## Common Discrepancy Patterns

Understanding why discrepancies occur helps with automated diagnosis:

### Shipping Not Mapped
**Pattern:** Shopify total > NetSuite total by exactly the shipping amount
**Cause:** Shipping line items not being created in NetSuite
**Fix:** Add shipping item mapping in the sync pipeline

### Tax Calculation Differences
**Pattern:** Small tax differences ($0.01-$0.10) on many orders
**Cause:** Shopify and NetSuite use different tax calculation methods (line-level vs. order-level rounding)
**Fix:** Usually acceptable; set tolerance threshold

### Refund Timing
**Pattern:** Order shows as fully paid in Shopify but has partial payment in NetSuite
**Cause:** Refund processed in Shopify but not yet synced to NetSuite
**Fix:** Check refund sync pipeline; may be a timing issue

### Multi-Currency Rounding
**Pattern:** Small amount differences on international orders
**Cause:** Exchange rate differences between Shopify and NetSuite
**Fix:** Ensure both systems use the same exchange rate source

### Gateway Fees
**Pattern:** NetSuite deposit amount < Shopify payout amount
**Cause:** Payment processor fees deducted from payout
**Fix:** Record processor fees as an expense in NetSuite

### Discount Code Mapping
**Pattern:** Shopify total < NetSuite total
**Cause:** Shopify discount not applied as a discount item in NetSuite
**Fix:** Map Shopify discount codes to NetSuite promotion items

## Building the Reconciliation Engine

### Implementation Pattern

The recon engine should follow the existing Celery task pattern:

```python
@celery_app.task(base=InstrumentedTask, bind=True, queue="recon")
def run_reconciliation(self, tenant_id: str, date_from: str, date_to: str, **kwargs):
    with tenant_session(tenant_id) as db:
        # 1. Fetch Shopify canonical records
        shopify_orders = fetch_canonical_orders(db, tenant_id, date_from, date_to)

        # 2. Fetch NetSuite records via SuiteQL
        netsuite_records = fetch_netsuite_transactions(db, tenant_id, date_from, date_to)

        # 3. Build match index
        matches, unmatched_shopify, unmatched_netsuite = match_records(
            shopify_orders, netsuite_records
        )

        # 4. Compare matched records
        discrepancies = compare_matched_records(matches)

        # 5. Add unmatched as discrepancies
        discrepancies.extend(classify_unmatched(unmatched_shopify, "missing_in_netsuite"))
        discrepancies.extend(classify_unmatched(unmatched_netsuite, "missing_in_ecommerce"))

        # 6. Save results
        run = save_recon_run(db, tenant_id, discrepancies, date_from, date_to)

    return {"run_id": str(run.id), "findings_count": len(discrepancies)}
```

### Data Model for Results

Consider these models for persisting reconciliation results:

```python
class ReconRun:
    id: UUID
    tenant_id: UUID
    date_from: date
    date_to: date
    status: str  # pending, running, completed, failed
    summary_json: dict  # Aggregate stats
    created_at: datetime
    completed_at: datetime | None

class ReconFinding:
    id: UUID
    run_id: UUID  # FK to ReconRun
    finding_type: str  # missing_in_netsuite, amount_mismatch, etc.
    severity: str  # high, medium, low
    shopify_ref: str | None  # Shopify order reference
    netsuite_ref: str | None  # NetSuite transaction reference
    expected_amount: Decimal | None
    actual_amount: Decimal | None
    difference: Decimal | None
    details_json: dict  # Full context
    resolution: str | None  # How it was resolved
    resolved_at: datetime | None
```

## SuiteQL Queries for Reconciliation

When querying NetSuite for reconciliation data, remember the SuiteQL rules:

```sql
-- Get sales orders with line details for matching
SELECT t.id, t.tranid, t.trandate, t.foreigntotal,
       t.externalid, t.otherrefnum, t.status,
       tl.item, tl.netamount, tl.quantity, tl.rate
FROM transaction t
JOIN transactionline tl ON t.id = tl.transaction
WHERE t.type = 'SalesOrd'
  AND t.trandate >= TO_DATE('2024-01-01', 'YYYY-MM-DD')
  AND t.trandate <= TO_DATE('2024-01-31', 'YYYY-MM-DD')
  AND tl.mainline = 'F'
  AND ROWNUM <= 1000

-- Get customer payments for deposit matching
SELECT t.id, t.tranid, t.trandate, t.foreigntotal,
       t.entity, NVL(t.externalid, '') as externalid
FROM transaction t
WHERE t.type = 'CustPymt'
  AND t.trandate >= TO_DATE('2024-01-01', 'YYYY-MM-DD')
  AND ROWNUM <= 1000

-- Get deposits for payout matching
SELECT t.id, t.tranid, t.trandate, t.foreigntotal
FROM transaction t
WHERE t.type = 'Deposit'
  AND t.trandate >= TO_DATE('2024-01-01', 'YYYY-MM-DD')
  AND ROWNUM <= 1000
```

## Reporting

Reconciliation results should be exportable via the `report.export` tool:
- **CSV format** — for spreadsheet analysis
- **JSON format** — for programmatic consumption
- **Summary format** — high-level stats for dashboards

Key metrics to track over time:
- Match rate (% of orders successfully matched)
- Discrepancy rate (% of matched orders with issues)
- Total discrepancy amount ($)
- Average resolution time
- Most common discrepancy types
