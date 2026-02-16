# Data Model Overview
_Last updated: 2026-02-16_

This document describes the complete Phase 1 data model: all tables, columns, types, constraints, indexes, RLS policies, and relationships.

---

## Entity Relationship Diagram

```
                                  +------------------+
                                  |     tenants      |
                                  +------------------+
                                  | id (PK, UUID)    |
                                  | name             |
                                  | slug (UNIQUE)    |
                                  | plan             |
                                  | plan_expires_at  |
                                  | is_active        |
                                  | created_at       |
                                  | updated_at       |
                                  +--------+---------+
                                           |
                    +-------------+--------+--------+------------------+
                    |             |                  |                  |
           +--------v-------+  +-v-----------+  +---v-----------+  +--v--------------+
           |  tenant_configs |  |    users    |  | connections   |  |     jobs        |
           +----------------+  +-------------+  +---------------+  +-----------------+
           | id (PK)        |  | id (PK)     |  | id (PK)       |  | id (PK)         |
           | tenant_id (FK) |  | tenant_id   |  | tenant_id(FK) |  | tenant_id       |
           | subsidiaries   |  | email       |  | provider      |  | job_type        |
           | account_       |  | hashed_pwd  |  | label         |  | status          |
           |  mappings      |  | full_name   |  | status        |  | correlation_id  |
           | posting_mode   |  | actor_type  |  | encrypted_    |  | connection_id   |
           | posting_batch_ |  | is_active   |  |  credentials  |  | started_at      |
           |  size          |  | created_at  |  | encryption_   |  | completed_at    |
           | posting_attach |  | updated_at  |  |  key_version  |  | parameters      |
           |  _evidence     |  +------+------+  | metadata_json |  | result_summary  |
           | netsuite_      |         |         | created_by    |  | error_message   |
           |  account_id    |         |         | created_at    |  | celery_task_id  |
           | created_at     |         |         | updated_at    |  | created_at      |
           | updated_at     |  +------v------+  +---------------+  | updated_at      |
           +----------------+  |  user_roles  |                    +-----------------+
                               +--------------+
                               | id (PK)      |
                               | tenant_id    |       +------------+
                               | user_id (FK) +------>|   roles    |
                               | role_id (FK) |       +------------+
                               | created_at   |       | id (PK)    |
                               | updated_at   |       | name (UQ)  |
                               +--------------+       +-----+------+
                                                            |
                                                    +-------v----------+
                                                    | role_permissions  |
                                                    +------------------+
                                                    | role_id (PK,FK)  |
                                                    | permission_id    |
                                                    |   (PK, FK)       |
                                                    +--------+---------+
                                                             |
                                                    +--------v---------+
                                                    |   permissions    |
                                                    +------------------+
                                                    | id (PK)          |
                                                    | codename (UQ)    |
                                                    +------------------+


  +------------------+    +------------------+    +-------------------+
  | audit_events     |    | canonical_orders |    | canonical_payouts |
  +------------------+    +------------------+    +-------------------+
  | id (PK, BIGINT)  |    | id (PK)          |    | id (PK)           |
  | tenant_id        |    | tenant_id        |    | tenant_id         |
  | timestamp        |    | dedupe_key (UQ)  |    | dedupe_key (UQ)   |
  | actor_id         |    | source           |    | source            |
  | actor_type       |    | source_id        |    | source_id         |
  | category         |    | order_number     |    | payout_id         |
  | action           |    | status           |    | status            |
  | resource_type    |    | currency         |    | currency          |
  | resource_id      |    | total_amount     |    | gross_amount      |
  | correlation_id   |    | subtotal         |    | fee_amount        |
  | job_id           |    | tax_amount       |    | net_amount         |
  | payload          |    | discount_amount  |    | arrival_date      |
  | status           |    | customer_email   |    | transaction_count |
  | error_message    |    | source_created   |    | source_created    |
  +------------------+    | synced_at        |    | synced_at         |
                          | created_at       |    | created_at        |
                          | updated_at       |    | updated_at        |
                          +------------------+    +-------------------+

  +--------------------+   +--------------------+   +-------------------+
  | canonical_refunds  |   | canonical_fees     |   | canonical_disputes|
  +--------------------+   +--------------------+   +-------------------+
  | id (PK)            |   | id (PK)            |   | id (PK)           |
  | tenant_id          |   | tenant_id          |   | tenant_id         |
  | dedupe_key (UQ)    |   | dedupe_key (UQ)    |   | dedupe_key (UQ)   |
  | source             |   | source             |   | source            |
  | source_id          |   | source_id          |   | source_id         |
  | order_id           |   | payout_id          |   | charge_id         |
  | reason             |   | fee_type           |   | reason            |
  | amount             |   | amount             |   | amount            |
  | currency           |   | currency           |   | currency          |
  | status             |   | description        |   | status            |
  | source_created     |   | source_created     |   | source_created    |
  | synced_at          |   | synced_at          |   | synced_at         |
  | created_at         |   | created_at         |   | created_at        |
  | updated_at         |   | updated_at         |   | updated_at        |
  +--------------------+   +--------------------+   +-------------------+

  +---------------------------+    +----------------------+
  | canonical_ns_transactions |    |    sync_cursors      |
  +---------------------------+    +----------------------+
  | id (PK)                   |    | id (PK)              |
  | tenant_id                 |    | tenant_id            |
  | dedupe_key (UQ)           |    | connection_id (FK)   |
  | transaction_type          |    | object_type          |
  | internal_id               |    | cursor_value         |
  | tran_id                   |    | cursor_type          |
  | tran_date                 |    | last_synced_at       |
  | posting_period            |    | created_at           |
  | account_id                |    | updated_at           |
  | amount                    |    +----------------------+
  | currency                  |
  | subsidiary_id             |
  | memo                      |
  | source_created            |
  | synced_at                 |
  | created_at                |
  | updated_at                |
  +---------------------------+
```

---

## Table Definitions

### System Tables

#### `tenants`

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK, default uuid4 | Tenant identifier |
| `name` | VARCHAR(255) | NOT NULL | Organization display name |
| `slug` | VARCHAR(255) | UNIQUE, NOT NULL | URL-safe identifier |
| `plan` | VARCHAR(50) | NOT NULL, default `'trial'` | Current plan: `trial`, `pro` |
| `plan_expires_at` | TIMESTAMPTZ | NULLABLE | Trial expiration date |
| `is_active` | BOOLEAN | NOT NULL, default `true` | Soft-delete / suspend flag |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` | Row creation time |
| `updated_at` | TIMESTAMPTZ | NOT NULL, default `now()` | Last modification time |

**Indexes:** Primary key on `id`, unique index on `slug`.

#### `tenant_configs`

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK | Config record identifier |
| `tenant_id` | UUID | UNIQUE, NOT NULL, FK -> tenants.id | One config per tenant |
| `subsidiaries` | JSONB | NULLABLE | List of subsidiary configurations |
| `account_mappings` | JSONB | NULLABLE | Account mapping rules per subsidiary |
| `posting_mode` | VARCHAR(50) | NOT NULL, default `'lumpsum'` | `lumpsum` or `detail` |
| `posting_batch_size` | INTEGER | NOT NULL, default `100` | Max lines per journal entry batch |
| `posting_attach_evidence` | BOOLEAN | NOT NULL, default `false` | Attach evidence CSV to JE |
| `netsuite_account_id` | VARCHAR(255) | NULLABLE | NetSuite account identifier |
| `created_at` | TIMESTAMPTZ | NOT NULL | Row creation time |
| `updated_at` | TIMESTAMPTZ | NOT NULL | Last modification time |

**Indexes:** Unique index on `tenant_id`.

#### `users`

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK | User identifier |
| `tenant_id` | UUID | NOT NULL, FK -> tenants.id | Owning tenant |
| `email` | VARCHAR(255) | NOT NULL | User email |
| `hashed_password` | VARCHAR(255) | NOT NULL | bcrypt hash |
| `full_name` | VARCHAR(255) | NOT NULL | Display name |
| `actor_type` | VARCHAR(50) | NOT NULL, default `'user'` | `user` or `service_account` |
| `is_active` | BOOLEAN | NOT NULL, default `true` | Active flag |
| `created_at` | TIMESTAMPTZ | NOT NULL | Row creation time |
| `updated_at` | TIMESTAMPTZ | NOT NULL | Last modification time |

**Indexes:** Index on `tenant_id`. Unique constraint on `(tenant_id, email)`.

#### `roles`

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK | Role identifier |
| `name` | VARCHAR(50) | UNIQUE, NOT NULL | Role name: `admin`, `finance`, `ops`, `readonly` |

**Seed data:** `admin`, `finance`, `ops`, `readonly`.

#### `permissions`

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK | Permission identifier |
| `codename` | VARCHAR(100) | UNIQUE, NOT NULL | Permission codename |

**Seed data:** `connections:read`, `connections:write`, `tables:read`, `tables:export`, `config:read`, `config:write`, `users:manage`, `audit:read`, `jobs:read`, `jobs:write`, `mcp_tools:invoke`.

#### `role_permissions`

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `role_id` | UUID | PK, FK -> roles.id | Role reference |
| `permission_id` | UUID | PK, FK -> permissions.id | Permission reference |

**Composite primary key** on `(role_id, permission_id)`.

#### `user_roles`

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK | Assignment identifier |
| `tenant_id` | UUID | NOT NULL, FK -> tenants.id | Tenant scope |
| `user_id` | UUID | NOT NULL, FK -> users.id | User reference |
| `role_id` | UUID | NOT NULL, FK -> roles.id | Role reference |
| `created_at` | TIMESTAMPTZ | NOT NULL | Assignment time |
| `updated_at` | TIMESTAMPTZ | NOT NULL | Last modification time |

**Indexes:** Index on `tenant_id`, `user_id`, `role_id`.

#### `connections`

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK | Connection identifier |
| `tenant_id` | UUID | NOT NULL, FK -> tenants.id | Owning tenant |
| `provider` | VARCHAR(50) | NOT NULL | `netsuite`, `shopify`, `stripe` |
| `label` | VARCHAR(255) | NOT NULL | User-defined label |
| `status` | VARCHAR(50) | NOT NULL, default `'active'` | `active`, `error`, `revoked` |
| `encrypted_credentials` | TEXT | NOT NULL | Fernet-encrypted credential blob |
| `encryption_key_version` | INTEGER | NOT NULL, default `1` | Key version for rotation |
| `metadata_json` | JSONB | NULLABLE | Provider-specific metadata |
| `created_by` | UUID | NULLABLE, FK -> users.id | Creating user |
| `created_at` | TIMESTAMPTZ | NOT NULL | Row creation time |
| `updated_at` | TIMESTAMPTZ | NOT NULL | Last modification time |

**Indexes:** Index on `tenant_id`.

#### `jobs`

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK | Job identifier |
| `tenant_id` | UUID | NOT NULL | Owning tenant |
| `job_type` | VARCHAR(100) | NOT NULL | `shopify_sync`, `stripe_sync`, `netsuite_sync`, etc. |
| `status` | VARCHAR(50) | NOT NULL, default `'pending'` | `pending`, `running`, `completed`, `failed` |
| `correlation_id` | VARCHAR(255) | NULLABLE | Correlation ID for tracing |
| `connection_id` | UUID | NULLABLE, FK -> connections.id | Associated connection |
| `started_at` | TIMESTAMPTZ | NULLABLE | When worker started processing |
| `completed_at` | TIMESTAMPTZ | NULLABLE | When processing finished |
| `parameters` | JSONB | NULLABLE | Job input parameters |
| `result_summary` | JSONB | NULLABLE | `{rows_processed, rows_created, rows_updated}` |
| `error_message` | TEXT | NULLABLE | Error details on failure |
| `celery_task_id` | VARCHAR(255) | NULLABLE | Celery task reference |
| `created_at` | TIMESTAMPTZ | NOT NULL | Row creation time |
| `updated_at` | TIMESTAMPTZ | NOT NULL | Last modification time |

**Indexes:** Index on `tenant_id`, `correlation_id`.

#### `audit_events`

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | BIGINT | PK, AUTOINCREMENT | Sequential event ID |
| `tenant_id` | UUID | NOT NULL | Owning tenant |
| `timestamp` | TIMESTAMPTZ | NOT NULL, default `now()` | Event time |
| `actor_id` | UUID | NULLABLE | User or service account |
| `actor_type` | VARCHAR(50) | NOT NULL, default `'user'` | `user`, `service_account`, `system` |
| `category` | VARCHAR(100) | NOT NULL | Event category |
| `action` | VARCHAR(100) | NOT NULL | Specific action |
| `resource_type` | VARCHAR(100) | NULLABLE | Affected resource type |
| `resource_id` | VARCHAR(255) | NULLABLE | Affected resource identifier |
| `correlation_id` | VARCHAR(255) | NULLABLE | Cross-service correlation |
| `job_id` | UUID | NULLABLE | Associated job |
| `payload` | JSONB | NULLABLE | Event-specific data |
| `status` | VARCHAR(50) | NOT NULL, default `'success'` | `success`, `error`, `denied` |
| `error_message` | TEXT | NULLABLE | Error details |

**Indexes:** Index on `tenant_id`, `category`, `action`, `correlation_id`.

**Append-only:** No UPDATE or DELETE operations permitted on this table. Consider adding a trigger to prevent modifications.

### Canonical Tables

All canonical tables share these common columns:

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK | Record identifier |
| `tenant_id` | UUID | NOT NULL | Owning tenant (RLS key) |
| `dedupe_key` | VARCHAR(512) | UNIQUE | Deterministic deduplication key |
| `source` | VARCHAR(50) | NOT NULL | Source system: `shopify`, `stripe`, `netsuite` |
| `source_id` | VARCHAR(255) | NOT NULL | ID in the source system |
| `source_created` | TIMESTAMPTZ | NULLABLE | Creation time in source |
| `synced_at` | TIMESTAMPTZ | NOT NULL | When this row was last synced |
| `created_at` | TIMESTAMPTZ | NOT NULL | Row creation time |
| `updated_at` | TIMESTAMPTZ | NOT NULL | Last modification time |

#### `canonical_orders`

Additional columns:

| Column | Type | Description |
|--------|------|-------------|
| `order_number` | VARCHAR(100) | Human-readable order number |
| `status` | VARCHAR(50) | `open`, `closed`, `cancelled` |
| `currency` | VARCHAR(10) | ISO 4217 currency code |
| `total_amount` | NUMERIC(15,2) | Order total |
| `subtotal` | NUMERIC(15,2) | Subtotal before tax/discount |
| `tax_amount` | NUMERIC(15,2) | Tax amount |
| `discount_amount` | NUMERIC(15,2) | Discount amount |
| `customer_email` | VARCHAR(255) | Customer email |

#### `canonical_payouts`

Additional columns:

| Column | Type | Description |
|--------|------|-------------|
| `payout_id` | VARCHAR(255) | Provider payout ID |
| `status` | VARCHAR(50) | `pending`, `in_transit`, `paid`, `failed`, `cancelled` |
| `currency` | VARCHAR(10) | ISO 4217 currency code |
| `gross_amount` | NUMERIC(15,2) | Gross payout amount |
| `fee_amount` | NUMERIC(15,2) | Total fees |
| `net_amount` | NUMERIC(15,2) | Net amount (gross - fees) |
| `arrival_date` | DATE | Expected or actual arrival date |
| `transaction_count` | INTEGER | Number of transactions in payout |

#### `canonical_refunds`

Additional columns:

| Column | Type | Description |
|--------|------|-------------|
| `order_id` | VARCHAR(255) | Associated order ID |
| `reason` | VARCHAR(255) | Refund reason |
| `amount` | NUMERIC(15,2) | Refund amount |
| `currency` | VARCHAR(10) | ISO 4217 currency code |
| `status` | VARCHAR(50) | `pending`, `completed`, `failed` |

#### `canonical_fees`

Additional columns:

| Column | Type | Description |
|--------|------|-------------|
| `payout_id` | VARCHAR(255) | Associated payout |
| `fee_type` | VARCHAR(100) | Fee type: `processing`, `platform`, `refund`, `chargeback`, `other` |
| `amount` | NUMERIC(15,2) | Fee amount |
| `currency` | VARCHAR(10) | ISO 4217 currency code |
| `description` | TEXT | Fee description |

#### `canonical_disputes`

Additional columns:

| Column | Type | Description |
|--------|------|-------------|
| `charge_id` | VARCHAR(255) | Associated charge/payment |
| `reason` | VARCHAR(255) | Dispute reason |
| `amount` | NUMERIC(15,2) | Disputed amount |
| `currency` | VARCHAR(10) | ISO 4217 currency code |
| `status` | VARCHAR(50) | `warning_needs_response`, `needs_response`, `under_review`, `won`, `lost` |

#### `canonical_ns_transactions`

Additional columns:

| Column | Type | Description |
|--------|------|-------------|
| `transaction_type` | VARCHAR(100) | `deposit`, `journal_entry`, `customer_payment`, `cash_sale`, etc. |
| `internal_id` | VARCHAR(100) | NetSuite internal ID |
| `tran_id` | VARCHAR(100) | NetSuite transaction number |
| `tran_date` | DATE | Transaction date |
| `posting_period` | VARCHAR(100) | NetSuite posting period |
| `account_id` | VARCHAR(100) | GL account ID |
| `amount` | NUMERIC(15,2) | Transaction amount |
| `currency` | VARCHAR(10) | ISO 4217 currency code |
| `subsidiary_id` | VARCHAR(100) | NetSuite subsidiary ID |
| `memo` | TEXT | Transaction memo |

#### `sync_cursors`

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK | Cursor identifier |
| `tenant_id` | UUID | NOT NULL | Owning tenant |
| `connection_id` | UUID | NOT NULL, FK -> connections.id | Associated connection |
| `object_type` | VARCHAR(100) | NOT NULL | `orders`, `payouts`, `refunds`, etc. |
| `cursor_value` | TEXT | NOT NULL | Last cursor/timestamp/offset |
| `cursor_type` | VARCHAR(50) | NOT NULL | `timestamp`, `offset`, `cursor_string` |
| `last_synced_at` | TIMESTAMPTZ | NOT NULL | When cursor was last advanced |
| `created_at` | TIMESTAMPTZ | NOT NULL | Row creation time |
| `updated_at` | TIMESTAMPTZ | NOT NULL | Last modification time |

**Unique constraint:** `(tenant_id, connection_id, object_type)`.

---

## Dedupe Key Format

All canonical tables use a deterministic dedupe key to ensure idempotent upserts:

```
Format: {tenant_id}:{source}:{object_type}:{source_id}
```

**Examples:**

| Table | Dedupe Key Example |
|-------|-------------------|
| canonical_orders | `550e8400-...:shopify:order:4832901234` |
| canonical_payouts | `550e8400-...:stripe:payout:po_1NqBz2...` |
| canonical_refunds | `550e8400-...:shopify:refund:8812349012` |
| canonical_fees | `550e8400-...:stripe:fee:txn_1NqBz2..._fee` |
| canonical_disputes | `550e8400-...:stripe:dispute:dp_1NqBz2...` |
| canonical_ns_transactions | `550e8400-...:netsuite:transaction:12345` |

**Properties:**
- Deterministic: same source record always produces the same key
- Globally unique within a tenant (enforced by UNIQUE constraint)
- Enables safe UPSERT (INSERT ... ON CONFLICT (dedupe_key) DO UPDATE)
- Supports backfill replay without creating duplicates

---

## Index Strategy

### Primary Indexes (Automatic)

Every table has a primary key index (UUID or BIGINT).

### Tenant Isolation Indexes

Every multi-tenant table has an index on `tenant_id`. This supports RLS policy evaluation performance.

### Query Performance Indexes

| Table | Index | Purpose |
|-------|-------|---------|
| `canonical_orders` | `(tenant_id, source_created DESC)` | Table view default sort |
| `canonical_orders` | `(tenant_id, status)` | Filter by status |
| `canonical_payouts` | `(tenant_id, arrival_date DESC)` | Payout timeline queries |
| `canonical_payouts` | `(tenant_id, status)` | Filter by status |
| `canonical_refunds` | `(tenant_id, source_created DESC)` | Table view default sort |
| `canonical_ns_transactions` | `(tenant_id, tran_date DESC)` | Transaction timeline queries |
| `canonical_ns_transactions` | `(tenant_id, transaction_type)` | Filter by type |
| `audit_events` | `(tenant_id, timestamp DESC)` | Audit log view |
| `audit_events` | `(tenant_id, category)` | Filter by category |
| `audit_events` | `(correlation_id)` | Trace lookup |
| `jobs` | `(tenant_id, created_at DESC)` | Job list view |
| `jobs` | `(tenant_id, status)` | Filter by status |
| `sync_cursors` | `(tenant_id, connection_id, object_type)` UNIQUE | Cursor lookup |

### Dedupe Indexes

All canonical tables have a UNIQUE index on `dedupe_key` to support `ON CONFLICT` upserts.

---

## RLS Policy Summary

All multi-tenant tables have RLS enabled with the following policy pattern:

```sql
-- Enable RLS
ALTER TABLE <table_name> ENABLE ROW LEVEL SECURITY;

-- Force RLS for table owner too
ALTER TABLE <table_name> FORCE ROW LEVEL SECURITY;

-- Tenant isolation policy
CREATE POLICY tenant_isolation ON <table_name>
    USING (tenant_id = current_setting('app.current_tenant_id')::uuid);
```

### Tables with RLS

| Table | RLS Enabled | Policy |
|-------|-------------|--------|
| `tenants` | No | Accessed by system queries only |
| `tenant_configs` | Yes | `tenant_id = current_setting(...)` |
| `users` | Yes | `tenant_id = current_setting(...)` |
| `user_roles` | Yes | `tenant_id = current_setting(...)` |
| `connections` | Yes | `tenant_id = current_setting(...)` |
| `jobs` | Yes | `tenant_id = current_setting(...)` |
| `audit_events` | Yes | `tenant_id = current_setting(...)` |
| `canonical_orders` | Yes | `tenant_id = current_setting(...)` |
| `canonical_payouts` | Yes | `tenant_id = current_setting(...)` |
| `canonical_refunds` | Yes | `tenant_id = current_setting(...)` |
| `canonical_fees` | Yes | `tenant_id = current_setting(...)` |
| `canonical_disputes` | Yes | `tenant_id = current_setting(...)` |
| `canonical_ns_transactions` | Yes | `tenant_id = current_setting(...)` |
| `sync_cursors` | Yes | `tenant_id = current_setting(...)` |
| `roles` | No | Global reference data |
| `permissions` | No | Global reference data |
| `role_permissions` | No | Global reference data |

---

## Table Count Summary

| Category | Tables | Count |
|----------|--------|-------|
| System | tenants, tenant_configs, users, roles, permissions, role_permissions, user_roles, connections, jobs, audit_events, sync_cursors | 11 |
| Canonical | canonical_orders, canonical_payouts, canonical_refunds, canonical_fees, canonical_disputes, canonical_ns_transactions | 6 |
| **Total** | | **17** |
