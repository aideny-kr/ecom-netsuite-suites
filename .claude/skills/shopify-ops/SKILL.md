---
name: shopify-ops
description: >
  Shopify ecommerce operations — sync management, order ingestion, data mapping to NetSuite,
  and troubleshooting Shopify integration issues. Use this skill whenever the user mentions
  Shopify, Shopify orders, Shopify sync, ecommerce sync, order import, payment ingestion,
  refund processing, Shopify webhooks, shop domain configuration, or any data flow between
  Shopify and NetSuite. Also trigger when the user asks about order discrepancies, missing
  orders, sync failures, cursor resets, or ecommerce data pipelines. If they mention any
  ecommerce platform integration with NetSuite, this skill likely applies.
---

# Shopify Ops

You are an expert in Shopify-to-NetSuite ecommerce operations. You understand the full data
pipeline from Shopify order creation through ingestion, canonical mapping, and NetSuite record
creation. You can diagnose sync issues, optimize data flows, and extend the integration.

## Architecture Overview

The Shopify sync pipeline in this project works like this:

```
Shopify Store API
    ↓ (REST API polling with cursor tracking)
Ingestion Service (shopify_sync.py)
    ↓ (upsert to canonical tables)
Canonical Data Layer (orders, payments, refunds)
    ↓ (Celery task on "sync" queue)
NetSuite Record Creation (via SuiteQL / MCP tools)
```

### Key Backend Files

- `backend/app/services/ingestion/shopify_sync.py` — Core sync logic
- `backend/app/workers/tasks/shopify_sync.py` — Celery task wrapper
- `backend/app/models/connection.py` — Shopify connection with encrypted OAuth credentials
- `backend/app/services/mcp_connector_service.py` — MCP connector management

## Sync Engine Details

### How Shopify Sync Works

The sync engine uses **incremental polling with cursor tracking**:

1. **Load Connection** — Decrypt Shopify OAuth token from the `connections` table
2. **Load Cursor** — Get the last sync timestamp (tracks where we left off)
3. **Paginate API** — Fetch orders/payments/refunds since the cursor, 250 per page
4. **Upsert Canonical Records** — Idempotent writes using dedup key `shopify:{order_id}`
5. **Save Cursor** — Update checkpoint for next run

### Shopify API Details

- **Base URL**: `https://{shop_domain}/admin/api/2024-01`
- **Auth Header**: `X-Shopify-Access-Token: {decrypted_token}`
- **Pagination**: Link header with `rel="next"` — follow until no next page
- **Page Size**: 250 records (Shopify maximum)
- **Rate Limits**: 2 requests/second (leaky bucket, 40 request pool)

### Key Endpoints Used

| Endpoint | Purpose |
|----------|---------|
| `/orders.json?updated_at_min={cursor}&status=any` | Fetch orders updated since last sync |
| `/orders/{id}/transactions.json` | Payment transactions for an order |
| `/orders/{id}/refunds.json` | Refund details |

### Celery Task Pattern

```python
@celery_app.task(base=InstrumentedTask, bind=True, queue="sync")
def shopify_sync(self, tenant_id: str, connection_id: str, **kwargs):
    with tenant_session(tenant_id) as db:
        result = sync_shopify(db, connection_id, tenant_id)
    return result
```

The task runs on the `sync` queue, which is separate from `default`, `recon`, and `export` queues for workload isolation.

## Shopify-to-NetSuite Field Mapping

### Order Mapping

| Shopify Field | NetSuite Field | Notes |
|--------------|---------------|-------|
| `order.id` | `externalid` | Dedup key: `shopify:{id}` |
| `order.name` | `otherrefnum` | Human-readable order number (#1001) |
| `order.email` | Customer lookup | Match or create customer |
| `order.created_at` | `trandate` | Order date |
| `order.total_price` | Total validation | Cross-check line totals |
| `order.line_items[].sku` | `item` (sublist) | Match by SKU to NetSuite item |
| `order.line_items[].quantity` | `quantity` | Line quantity |
| `order.line_items[].price` | `rate` | Unit price |
| `order.shipping_lines[].price` | Shipping item line | Map to a shipping item |
| `order.discount_codes[]` | Discount line | Map to discount item or promo code |
| `order.tax_lines[]` | Tax override | Map to tax codes |

### Customer Mapping

| Shopify Field | NetSuite Field |
|--------------|---------------|
| `customer.email` | `email` (primary match key) |
| `customer.first_name + last_name` | `companyname` or `firstname`/`lastname` |
| `customer.default_address` | `addressbook` sublist |
| `customer.phone` | `phone` |

### Payment Mapping

| Shopify Transaction | NetSuite Record |
|--------------------|----------------|
| `kind: "sale"` | Customer Payment |
| `kind: "refund"` | Customer Refund |
| `kind: "authorization"` | (Track for reconciliation) |

## Troubleshooting Guide

### Common Sync Issues

**Orders not syncing:**
1. Check the connection status — is it `active`? Are credentials valid?
2. Check the cursor — has it advanced? A stuck cursor means API errors
3. Check Shopify API rate limits — too many 429 responses will stall sync
4. Check Celery worker logs — is the `sync` queue being consumed?

**Duplicate orders in NetSuite:**
1. Check the dedup key — `shopify:{order_id}` should prevent duplicates
2. Check if `externalid` is being set on the NetSuite record
3. Check for race conditions — multiple workers processing the same order

**Missing line items:**
1. Check SKU mapping — does the Shopify SKU match a NetSuite item?
2. Check for deleted/inactive items in NetSuite
3. Check for line items with zero quantity (gift cards, tips)

**Payment mismatches:**
1. Shopify may have partial captures — check `transaction.kind` and `transaction.status`
2. Multi-currency orders need exchange rate handling
3. Shopify payments include gateway fees which aren't in the order total

### Cursor Management

The cursor is the sync checkpoint. If you need to re-sync data:

```python
# Reset cursor to re-sync from a specific date
# This will re-process all orders updated after the given date
# Existing records will be upserted (idempotent), not duplicated
```

To diagnose cursor issues, check:
- When was the cursor last updated?
- Does the cursor timestamp match the last successfully synced order?
- Is there a gap between the cursor and the oldest unsynced order?

## Extending the Integration

### Adding a New Shopify Resource

To sync a new Shopify resource (e.g., inventory levels, products):

1. Add a fetch function in `shopify_sync.py` following the pagination pattern
2. Define the canonical model and dedup key
3. Add the upsert logic
4. Update the Celery task to include the new resource
5. Add cursor tracking for the new resource type

### Adding Webhook Support

For real-time sync instead of polling:

1. Register webhooks via Shopify Admin API
2. Create a webhook endpoint in the FastAPI backend
3. Verify HMAC signatures for security
4. Queue processing via Celery (don't process inline — webhooks have a 5-second timeout)

### Multi-Store Support

The system supports multiple Shopify stores per tenant through the connections table. Each connection has its own:
- `shop_domain` and OAuth credentials
- Sync cursor (independent per store)
- Canonical records tagged with `connection_id`

## Schedule Configuration

Shopify sync can be scheduled via the app's scheduling system:
- Default: every 15 minutes
- Can be configured per tenant
- Manual trigger available via API
- Schedule configs in `backend/app/api/v1/schedules.py`
