---
topic_tags: ["suiteql", "custom-fields", "custom-records"]
source_type: documentation
---

# Custom Fields and Records

## Custom Field Naming Conventions

NetSuite custom fields follow predictable naming patterns:

| Prefix | Applied To | Example |
|--------|-----------|---------|
| `custbody_` | Transaction header (body) | `custbody_channel`, `custbody_shopify_order` |
| `custcol_` | Transaction line (column) | `custcol_lot_number` |
| `custitem_` | Item record | `custitem_fw_platform`, `custitem_brand` |
| `custentity_` | Entity (customer/vendor/employee) | `custentity_credit_limit` |
| `custevent_` | CRM event/activity | `custevent_follow_up` |
| `custrecord_` | Custom record field | `custrecord_processor_status` |

## Querying Custom Fields

Custom fields are queryable as regular columns on their parent tables:
```sql
-- Transaction body field
SELECT t.tranid, t.custbody_channel, BUILTIN.DF(t.custbody_channel) as channel_name
FROM transaction t
WHERE t.type = 'SalesOrd'
FETCH FIRST 10 ROWS ONLY

-- Item custom field
SELECT i.itemid, BUILTIN.DF(i.custitem_fw_platform) as platform
FROM item i
WHERE i.type = 'InvtPart'
FETCH FIRST 10 ROWS ONLY

-- Transaction line custom field
SELECT tl.id, tl.custcol_lot_number
FROM transactionline tl
WHERE tl.custcol_lot_number IS NOT NULL
FETCH FIRST 10 ROWS ONLY
```

## SELECT Fields and Custom Lists

Custom fields with type `SELECT` store integer IDs referencing custom lists:

```sql
-- Get the display value
SELECT BUILTIN.DF(t.custbody_channel) as channel_name FROM transaction t

-- Filter by ID (faster)
WHERE t.custbody_channel = 3

-- Filter by display name (readable)
WHERE BUILTIN.DF(t.custbody_channel) = 'Wholesale'
```

Check the tenant schema's Custom List Values section for the ID-to-name mappings.

## Custom Record Queries

Custom records are separate tables with the `customrecord_` prefix:

```sql
-- Step 1: Discover the structure
SELECT * FROM customrecord_r_inv_processor WHERE ROWNUM <= 5

-- Step 2: Query with known fields
SELECT id, name, custrecord_status, created
FROM customrecord_r_inv_processor
WHERE isinactive = 'F'
ORDER BY created DESC
FETCH FIRST 20 ROWS ONLY
```

Important: Custom record table names in SuiteQL are ALWAYS lowercase. Convert script IDs from metadata to lowercase before querying.

## Anti-Hallucination Rule

Never guess custom field or custom record IDs. If a field is not listed in the tenant schema or tenant vernacular:
1. Call `netsuite_get_metadata` to discover available fields
2. Use `rag_search` to search documentation
3. Try `SELECT * FROM <table> WHERE ROWNUM <= 1` to discover columns

Guessing field names (e.g., inventing `custitem_fw_platform`) wastes retries and tokens.
