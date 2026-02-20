# Data Model Overview
_Last updated: 2026-02-19_

This document describes the complete data model: all tables, their purpose, key columns, and relationships.

---

## Table Summary

| Category | Tables | Count |
|----------|--------|-------|
| System | tenants, tenant_configs, users, roles, permissions, role_permissions, user_roles | 7 |
| Auth & Connections | connections | 1 |
| Jobs & Scheduling | jobs, schedules, cursor_states, evidence_packs | 4 |
| Audit | audit_events | 1 |
| Canonical Data | orders, payments, refunds, payouts, payout_lines, disputes, netsuite_postings | 7 |
| Chat & AI | chat_sessions, chat_messages, doc_chunks, chat_api_keys | 4 |
| MCP | mcp_connectors | 1 |
| Workspace / IDE | workspaces, workspace_files, workspace_changesets, workspace_patches, workspace_runs, workspace_artifacts | 6 |
| Onboarding & Config | onboarding_checklist_items, tenant_profiles, policy_profiles, system_prompt_templates | 4 |
| NetSuite | netsuite_metadata, script_sync_states, netsuite_api_logs | 3 |
| **Total** | | **38** |

---

## Entity Relationship Diagram

```
                              +------------------+
                              |     tenants      |
                              +------------------+
                              | id (PK, UUID)    |
                              | name, slug (UQ)  |
                              | plan, is_active  |
                              +--------+---------+
                                       |
          +----------+--------+--------+--------+-----------+----------+
          |          |        |                  |           |          |
  +-------v----+ +--v------+ v-----------+ +---v-------+ +-v--------+ v-----------+
  |tenant_     | |  users  | connections | |   jobs    | |chat_     | |workspaces  |
  | configs    | +----+----+ +-----------+ +-----------+ |sessions  | +------------+
  +------------+      |                                   +----------+ |workspace_  |
  |ai_provider |  +---v--------+                          |session_  | | files      |
  |ai_model    |  | user_roles |                          | type     | |workspace_  |
  |multi_agent |  +------------+                          |workspace_| | changesets |
  | _enabled   |  | role_id FK |                          | id (FK?) | +------------+
  +------------+  +------+-----+                          +----+-----+
                         |                                     |
                  +------v------+                     +--------v--------+
                  |   roles     |                     | chat_messages   |
                  +------+------+                     +-----------------+
                         |                            | model_used      |
                  +------v----------+                 | provider_used   |
                  |role_permissions |                 | is_byok         |
                  +---------+-------+                 | input/output    |
                            |                         |  _tokens        |
                  +---------v-------+                 +-----------------+
                  |  permissions    |
                  +-----------------+

  +----------------+  +----------------+  +----------------+
  | doc_chunks     |  | mcp_connectors |  | netsuite_      |
  +----------------+  +----------------+  |  metadata       |
  | embedding      |  | server_url     |  +----------------+
  |  Vector(1024)  |  | auth_type      |  | transaction_   |
  | source_path    |  | oauth_*        |  |  body_fields   |
  +----------------+  +----------------+  | subsidiaries   |
                                          +----------------+
```

---

## Table Definitions

### System Tables

#### `tenants`

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK | Tenant identifier |
| `name` | VARCHAR(255) | NOT NULL | Organization display name |
| `slug` | VARCHAR(255) | UNIQUE, NOT NULL | URL-safe identifier |
| `plan` | VARCHAR(50) | NOT NULL, default `'trial'` | Current plan: `trial`, `pro` |
| `plan_expires_at` | TIMESTAMPTZ | NULLABLE | Trial expiration date |
| `is_active` | BOOLEAN | NOT NULL, default `true` | Soft-delete / suspend flag |
| `created_at` | TIMESTAMPTZ | NOT NULL | Row creation time |
| `updated_at` | TIMESTAMPTZ | NOT NULL | Last modification time |

#### `tenant_configs`

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK | Config record identifier |
| `tenant_id` | UUID | UNIQUE, FK -> tenants.id | One config per tenant |
| `subsidiaries` | JSONB | NULLABLE | Subsidiary configurations |
| `account_mappings` | JSONB | NULLABLE | Account mapping rules |
| `posting_mode` | VARCHAR(50) | default `'lumpsum'` | `lumpsum` or `detail` |
| `posting_batch_size` | INTEGER | default `100` | Max lines per JE batch |
| `posting_attach_evidence` | BOOLEAN | default `false` | Attach evidence CSV |
| `netsuite_account_id` | VARCHAR(255) | NULLABLE | NetSuite account ID |
| `ai_provider` | VARCHAR(50) | NULLABLE | BYOK: `openai`, `anthropic`, `gemini` |
| `ai_model` | VARCHAR(100) | NULLABLE | BYOK model identifier |
| `ai_api_key_encrypted` | TEXT | NULLABLE | Fernet-encrypted API key |
| `ai_key_version` | INTEGER | NULLABLE | Encryption key version |
| `multi_agent_enabled` | BOOLEAN | NULLABLE | Enable multi-agent coordinator |
| `onboarding_completed_at` | TIMESTAMPTZ | NULLABLE | When onboarding finished |

#### `users`

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK | User identifier |
| `tenant_id` | UUID | NOT NULL, FK -> tenants.id | Owning tenant |
| `email` | VARCHAR(255) | NOT NULL | User email |
| `hashed_password` | VARCHAR(255) | NOT NULL | bcrypt hash |
| `full_name` | VARCHAR(255) | NOT NULL | Display name |
| `actor_type` | VARCHAR(50) | default `'user'` | `user` or `service_account` |
| `is_active` | BOOLEAN | default `true` | Active flag |

**Unique constraint:** `(tenant_id, email)`.

#### `roles`, `permissions`, `role_permissions`, `user_roles`

Standard RBAC tables. Roles: `admin`, `finance`, `ops`, `readonly`. Permissions include `connections:read/write`, `tables:read/export`, `config:read/write`, `users:manage`, `audit:read`, `jobs:read/write`, `mcp_tools:invoke`.

### Auth & Connections

#### `connections`

| Column | Type | Constraints | Description |
|--------|------|------------|-------------|
| `id` | UUID | PK | Connection identifier |
| `tenant_id` | UUID | FK -> tenants.id | Owning tenant |
| `provider` | VARCHAR(50) | NOT NULL | `netsuite`, `shopify`, `stripe` |
| `label` | VARCHAR(255) | NOT NULL | User-defined label |
| `status` | VARCHAR(50) | default `'active'` | `active`, `error`, `revoked` |
| `auth_type` | VARCHAR(50) | default `'oauth2'` | `oauth2` or `tba` |
| `encrypted_credentials` | TEXT | NOT NULL | Fernet-encrypted credentials |
| `encryption_key_version` | INTEGER | default `1` | Key version for rotation |
| `metadata_json` | JSONB | NULLABLE | Provider-specific metadata |
| `created_by` | UUID | FK -> users.id | Creating user |

### Jobs & Scheduling

#### `jobs`

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID (PK) | Job identifier |
| `tenant_id` | UUID | Owning tenant |
| `job_type` | VARCHAR(100) | `shopify_sync`, `stripe_sync`, `netsuite_sync`, `metadata_discovery`, etc. |
| `status` | VARCHAR(50) | `pending`, `running`, `completed`, `failed` |
| `correlation_id` | VARCHAR(255) | Cross-service tracing |
| `connection_id` | UUID (FK) | Associated connection |
| `parameters` | JSONB | Job input parameters |
| `result_summary` | JSONB | `{rows_processed, rows_created, rows_updated}` |
| `error_message` | TEXT | Error details on failure |
| `celery_task_id` | VARCHAR(255) | Celery task reference |

#### `schedules`

Cron-based job schedules linked to connections.

#### `cursor_states`

Incremental sync cursors. Unique on `(tenant_id, connection_id, object_type)`.

#### `evidence_packs`

Immutable evidence CSV/attachment packs linked to jobs for audit trail.

### Audit

#### `audit_events`

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGINT (PK, AUTO) | Sequential event ID |
| `tenant_id` | UUID | Owning tenant |
| `timestamp` | TIMESTAMPTZ | Event time |
| `actor_id` | UUID | User or service account |
| `actor_type` | VARCHAR(50) | `user`, `service_account`, `system` |
| `category` | VARCHAR(100) | Event category |
| `action` | VARCHAR(100) | Specific action |
| `resource_type` | VARCHAR(100) | Affected resource type |
| `resource_id` | VARCHAR(255) | Affected resource ID |
| `correlation_id` | VARCHAR(255) | Cross-service correlation |
| `payload` | JSONB | Event-specific data |
| `status` | VARCHAR(50) | `success`, `error`, `denied` |

**Append-only:** No UPDATE or DELETE operations permitted.

### Canonical Data Tables

All canonical tables share: `id` (UUID PK), `tenant_id`, `dedupe_key` (UNIQUE), `source`, `source_id`, `source_created`, `synced_at`, `created_at`, `updated_at`.

**Dedupe key format:** `{tenant_id}:{source}:{object_type}:{source_id}`

| Table | Key Additional Columns |
|-------|----------------------|
| `orders` | order_number, status, currency, total_amount, subtotal, tax_amount, discount_amount, customer_email |
| `payments` | payment_id, order_id, amount, currency, status, payment_method |
| `refunds` | order_id, reason, amount, currency, status |
| `payouts` | payout_id, status, currency, gross_amount, fee_amount, net_amount, arrival_date, transaction_count |
| `payout_lines` | payout_id (FK), fee_type, amount, currency, description |
| `disputes` | charge_id, reason, amount, currency, status |
| `netsuite_postings` | transaction_type, internal_id, tran_id, tran_date, posting_period, account_id, amount, currency, subsidiary_id, memo |

### Chat & AI Tables

#### `chat_sessions`

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID (PK) | Session identifier |
| `tenant_id` | UUID | Owning tenant |
| `user_id` | UUID | Session owner |
| `title` | VARCHAR | Display title (auto-set from first message) |
| `session_type` | VARCHAR(20) | `chat` (dashboard), `workspace`, `onboarding` |
| `workspace_id` | UUID (nullable) | Links session to a specific workspace |
| `is_archived` | BOOLEAN | Soft archive flag |

**Session segregation:** Dashboard shows `session_type='chat'` + `workspace_id IS NULL`. Each workspace shows only its own sessions filtered by `workspace_id`.

#### `chat_messages`

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID (PK) | Message identifier |
| `tenant_id` | UUID | Owning tenant |
| `session_id` | UUID (FK) | Parent session |
| `role` | VARCHAR | `user`, `assistant`, `system` |
| `content` | TEXT | Message text |
| `tool_calls` | JSONB | Logged tool calls with params, results, durations |
| `citations` | JSONB | RAG citations |
| `token_count` | INTEGER | Total tokens |
| `input_tokens` | INTEGER | Input token count |
| `output_tokens` | INTEGER | Output token count |
| `model_used` | VARCHAR | Model identifier (e.g., `claude-sonnet-4-5-20250929`, `gpt-5.2`) |
| `provider_used` | VARCHAR | `anthropic`, `openai`, `gemini` |
| `is_byok` | BOOLEAN | Whether tenant's own API key was used |

#### `doc_chunks`

RAG vector store for documentation search.

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID (PK) | Chunk identifier |
| `tenant_id` | UUID | System tenant (`00000000-...`) for shared docs |
| `source_path` | VARCHAR | File path (e.g., `netsuite_docs/suiteql-syntax-reference.md`) |
| `title` | VARCHAR | Document title |
| `chunk_index` | INTEGER | Position within document |
| `content` | TEXT | Chunk text |
| `token_count` | INTEGER | Estimated token count |
| `embedding` | Vector(1024) | Voyage AI embedding (pgvector) |

#### `chat_api_keys`

Hashed API keys for external chat integration (`/api/v1/integration/chat`).

### MCP Tables

#### `mcp_connectors`

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID (PK) | Connector identifier |
| `tenant_id` | UUID | Owning tenant |
| `name` | VARCHAR | Display name (e.g., "NetSuite MCP") |
| `server_url` | VARCHAR | MCP server endpoint URL |
| `auth_type` | VARCHAR | `oauth2`, `api_key`, `none` |
| `oauth_client_id` | VARCHAR | OAuth client ID |
| `oauth_token_encrypted` | TEXT | Encrypted OAuth tokens |
| `status` | VARCHAR | `active`, `error`, `inactive` |
| `discovered_tools` | JSONB | Cached tool definitions from `list_tools()` |

### Workspace / IDE Tables

#### `workspaces`

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID (PK) | Workspace identifier |
| `tenant_id` | UUID | Owning tenant |
| `name` | VARCHAR | Workspace display name |
| `description` | TEXT | Optional description |

#### `workspace_files`

Virtual filesystem entries within a workspace.

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID (PK) | File identifier |
| `workspace_id` | UUID (FK) | Parent workspace |
| `path` | VARCHAR | File path within SDF project |
| `content` | TEXT | File content |
| `netsuite_file_id` | VARCHAR | NetSuite File Cabinet ID (changes on push) |

#### `workspace_changesets`

Diff-based change sets with approval state machine: `draft → pending_review → approved → applied`.

#### `workspace_patches`

Individual file patches (diffs) within a changeset.

#### `workspace_runs`

SDF validate, Jest test, and SuiteQL assertion execution records.

#### `workspace_artifacts`

Immutable stdout/stderr/report artifacts from runs.

### Onboarding & Configuration Tables

#### `tenant_profiles`

Business context profiles used to generate AI system prompts. Contains industry, description, subsidiaries, chart of accounts, item types, custom segments, fiscal calendar.

#### `policy_profiles`

Data governance policies: read_only_mode, allowed_record_types, blocked_fields, tool_allowlist, max_rows_per_query, custom_rules.

#### `system_prompt_templates`

Versioned generated system prompt templates built from tenant + policy profiles.

#### `onboarding_checklist_items`

Wizard step tracking: `connection`, `profile`, `policy`, `discovery`, `finalize`.

### NetSuite Tables

#### `netsuite_metadata`

Versioned snapshots of discovered NetSuite custom fields, record types, subsidiaries, departments, locations, classifications. Used to inject accurate schema info into SuiteQL agent prompts.

#### `script_sync_states`

Per-tenant SuiteScript file sync tracking (last sync time, file counts, status).

#### `netsuite_api_logs`

Full request/response logging for NetSuite REST API and MCP calls. Includes method, URL, status code, duration, request/response bodies.

---

## RLS Policy Summary

All multi-tenant tables have RLS enabled with:

```sql
ALTER TABLE <table_name> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <table_name> FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON <table_name>
    USING (tenant_id = current_setting('app.current_tenant_id')::uuid);
```

Global reference tables (`tenants`, `roles`, `permissions`, `role_permissions`) do not have RLS.

---

## Index Strategy

- **Tenant isolation:** Every multi-tenant table has an index on `tenant_id`
- **Dedupe:** All canonical tables have a UNIQUE index on `dedupe_key`
- **Query performance:** Composite indexes on frequently filtered columns (e.g., `(tenant_id, source_created DESC)`, `(tenant_id, status)`)
- **Vector search:** `doc_chunks.embedding` uses pgvector ivfflat or HNSW index for cosine similarity
- **Audit:** Indexes on `(tenant_id, timestamp DESC)`, `(correlation_id)`
