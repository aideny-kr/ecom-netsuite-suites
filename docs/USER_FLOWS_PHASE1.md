# Phase 1 User Flows
_Last updated: 2026-02-16_

This document describes step-by-step user flows for Phase 1 of the NetSuite Ecommerce Ops Suite. Each flow covers the UI interactions, API calls, and system side-effects.

---

## Flow 1: Onboarding (Register -> Add Connection -> View Tables)

### 1.1 Registration

```
User                        Frontend                      API                         Database
 |                             |                            |                             |
 |-- Fill registration form -->|                            |                             |
 |   (name, slug, email, pwd) |                            |                             |
 |                             |-- POST /auth/register ---->|                             |
 |                             |                            |-- INSERT tenants ---------->|
 |                             |                            |-- INSERT users ------------->|
 |                             |                            |-- INSERT tenant_configs ---->|
 |                             |                            |-- INSERT user_roles -------->|
 |                             |                            |-- INSERT audit_events ------>|
 |                             |                            |   (tenant_registered)        |
 |                             |<--- 201 {tokens, tenant} --|                             |
 |<-- Redirect to Dashboard ---|                            |                             |
```

**Steps:**

1. User navigates to `/register`.
2. User fills in: Organization Name, URL Slug, Admin Email, Password.
3. Frontend validates inputs (slug format, password strength, email format).
4. Frontend submits POST `/api/v1/auth/register`.
5. Backend creates tenant with `plan='trial'` and `plan_expires_at = now + 60 days`.
6. Backend creates admin user with bcrypt-hashed password.
7. Backend creates default `tenant_configs` (lumpsum posting, batch size 100).
8. Backend assigns `admin` role to the user.
9. Backend emits `tenant_registered` audit event.
10. Backend returns JWT access token + refresh token.
11. Frontend stores tokens and redirects to the Dashboard.
12. Dashboard shows onboarding checklist: "Connect NetSuite", "Connect a source", "View your data".

### 1.2 Add First Connection (NetSuite)

**Steps:**

1. User clicks "Add Connection" or the onboarding checklist item "Connect NetSuite".
2. User selects provider: NetSuite.
3. Form presents fields: Account ID, Consumer Key, Consumer Secret, Token ID, Token Secret (or OAuth 2.0 fields).
4. User fills in credentials and a label (e.g., "Production NetSuite").
5. Frontend submits POST `/api/v1/connections` with `{provider: 'netsuite', label, credentials}`.
6. Backend encrypts credentials with Fernet (current `ENCRYPTION_KEY_VERSION`).
7. Backend stores connection with `status='active'`.
8. Backend emits `connection_created` audit event.
9. Frontend shows success: "NetSuite connected" with a green status badge.
10. Onboarding checklist updates: "Connect NetSuite" marked complete.

### 1.3 Add Second Connection (Shopify or Stripe)

**Steps:**

1. User clicks "Add Connection" and selects Shopify (or Stripe).
2. For Shopify: User enters shop domain, API key, API secret.
3. For Stripe: User enters API key (or initiates OAuth flow).
4. Frontend submits POST `/api/v1/connections`.
5. Backend validates, encrypts, stores, audits (same as 1.2).
6. Onboarding checklist updates: "Connect a source" marked complete.

### 1.4 View Empty Canonical Tables

**Steps:**

1. User navigates to "Tables" in the sidebar.
2. Default view shows the Orders table.
3. Table displays column headers matching the canonical schema (order_id, source, status, amount, currency, created_at, etc.).
4. Table body shows empty state: "No data yet. Run a sync to populate this table."
5. User can switch between tables: Orders, Payouts, Refunds, Fees, Disputes.
6. Each table shows correct columns but zero rows.
7. A "Run Sync" button is visible, linking to the Jobs page.

---

## Flow 2: Table Explorer (Navigate -> Sort -> Filter -> Export CSV)

### 2.1 Navigate to a Table

**Steps:**

1. User clicks "Tables" in the left sidebar.
2. Sub-navigation shows available tables: Orders, Payouts, Refunds, Fees, Disputes, NS Transactions.
3. User clicks "Payouts".
4. Frontend calls GET `/api/v1/tables/payouts?page=1&page_size=50`.
5. Table renders with paginated data, showing total count in footer.

### 2.2 Sort by Column

**Steps:**

1. User clicks the "Amount" column header.
2. First click: sort ascending (arrow-up indicator).
3. Frontend calls GET `/api/v1/tables/payouts?sort_by=amount&sort_dir=asc&page=1`.
4. Table re-renders with sorted data.
5. Second click on same column: sort descending.
6. Third click: clear sort (return to default order).

### 2.3 Apply Filters

**Steps:**

1. User clicks "Filters" button above the table.
2. Filter panel slides open showing available filter fields:
   - Date range (from/to date pickers)
   - Status (dropdown: pending, completed, failed)
   - Source (dropdown: shopify, stripe)
   - Amount range (min/max number inputs)
   - Search (free-text for IDs)
3. User selects date range: "2026-01-01" to "2026-01-31".
4. User selects status: "completed".
5. User clicks "Apply Filters".
6. Frontend calls GET `/api/v1/tables/payouts?date_from=2026-01-01&date_to=2026-01-31&status=completed&page=1`.
7. Table updates, total count reflects filtered result.
8. Active filters shown as chips above the table (removable individually).

### 2.4 Export to CSV

**Steps:**

1. User clicks "Export" button above the table.
2. Dropdown shows options: "CSV" (available on trial), "Excel" (pro only, grayed out on trial).
3. User selects "CSV".
4. Frontend calls POST `/api/v1/tables/payouts/export` with `{format: 'csv', filters: {...current_filters}}`.
5. Backend checks entitlement: trial allows CSV.
6. Backend generates CSV with current filters applied (server-side, not limited to current page).
7. Backend emits `table_exported` audit event with `{table, format, row_count, filters}`.
8. Browser downloads `payouts_2026-02-16.csv`.
9. If trial user selects "Excel": modal shows "Upgrade to Pro for Excel exports" with upgrade CTA.

---

## Flow 3: Audit Viewer (View Events -> Filter by Category/Date/Correlation ID)

### 3.1 View Audit Events

**Steps:**

1. User clicks "Audit Log" in the sidebar (requires `admin` role or `audit:read` permission).
2. Frontend calls GET `/api/v1/audit?page=1&page_size=50&sort_by=timestamp&sort_dir=desc`.
3. Table renders audit events with columns:
   - Timestamp (human-readable with timezone)
   - Category (color-coded badge: auth=blue, connection=green, job=orange, mcp_tool=purple)
   - Action
   - Actor (email or "system")
   - Resource Type / Resource ID
   - Status (success=green, error=red, denied=yellow)
   - Correlation ID (truncated, clickable)
4. Most recent events appear first.

### 3.2 Filter by Category

**Steps:**

1. User clicks "Filters" above the audit table.
2. User selects Category: "connection".
3. Frontend calls GET `/api/v1/audit?category=connection&page=1`.
4. Table shows only connection-related events (created, tested, revoked).

### 3.3 Filter by Date Range

**Steps:**

1. User sets date range: last 7 days.
2. Frontend calls GET `/api/v1/audit?date_from=2026-02-09&date_to=2026-02-16&page=1`.
3. Table shows events within the specified window.

### 3.4 Filter by Correlation ID

**Steps:**

1. User clicks a `correlation_id` value in the table (e.g., from a job event).
2. Frontend calls GET `/api/v1/audit?correlation_id=abc-123-def&page=1`.
3. Table shows all events sharing that correlation ID, providing a complete trace of one logical operation.
4. Events are sorted chronologically (ascending) to show the operation flow.
5. This view answers: "What happened during this sync job?" showing job_started -> rows processed -> job_completed (or job_failed).

### 3.5 Event Detail

**Steps:**

1. User clicks an audit event row.
2. Side panel or modal opens showing full event detail:
   - All fields from the audit event record
   - `payload` JSON rendered as formatted, syntax-highlighted JSON
   - Related events (same correlation_id) listed below
3. User can copy the correlation_id or event details to clipboard.

---

## Flow 4: Connection Management (Add -> Test -> Revoke)

### 4.1 View Connections

**Steps:**

1. User navigates to "Connections" in the sidebar.
2. Frontend calls GET `/api/v1/connections`.
3. Page displays connection cards, each showing:
   - Provider icon and name (NetSuite, Shopify, Stripe)
   - Label (user-defined name)
   - Status badge (active=green, error=red, revoked=gray)
   - Created date
   - Last sync date (from most recent completed job, or "Never")
   - Actions menu (Test, Edit, Revoke)
4. Credentials are never displayed (redacted in API response).

### 4.2 Add a New Connection

**Steps:**

1. User clicks "Add Connection" button.
2. Modal or page shows provider selection: NetSuite, Shopify, Stripe.
3. User selects a provider.
4. Form renders provider-specific credential fields:
   - **NetSuite**: Account ID, Consumer Key, Consumer Secret, Token ID, Token Secret
   - **Shopify**: Shop Domain, API Key, API Secret (or OAuth redirect)
   - **Stripe**: API Key (or OAuth redirect)
5. User fills in credentials and a label.
6. User clicks "Save Connection".
7. Frontend submits POST `/api/v1/connections`.
8. Backend validates provider and required fields.
9. Backend encrypts credentials with Fernet.
10. Backend stores connection, emits audit event.
11. On success: connection card appears with "active" status.
12. On validation error: form shows field-level errors.

### 4.3 Test a Connection

**Steps:**

1. User clicks "Test" on an existing connection card.
2. Frontend calls POST `/api/v1/connections/{id}/test`.
3. Backend decrypts credentials.
4. Backend makes a lightweight API call to the provider:
   - **NetSuite**: Execute a minimal SuiteQL query (`SELECT 1`)
   - **Shopify**: Call shop info endpoint
   - **Stripe**: Call account info endpoint
5. Backend emits `connection_tested` audit event with result.
6. On success: toast notification "Connection healthy" with green checkmark.
7. On failure: toast notification with error detail (e.g., "Authentication failed: invalid token"). Connection status updated to `error`.

### 4.4 Revoke a Connection

**Steps:**

1. User clicks "Revoke" on a connection card.
2. Confirmation dialog: "Are you sure you want to revoke the connection '{label}'? This will stop all syncs using this connection."
3. User confirms.
4. Frontend calls DELETE `/api/v1/connections/{id}`.
5. Backend sets `status='revoked'` (soft delete -- credentials wiped, row retained for audit history).
6. Backend cancels any pending/running jobs associated with this connection.
7. Backend emits `connection_revoked` audit event.
8. Connection card shows "revoked" status in gray.
9. Revoked connections can be filtered out of the default view but remain visible in "Show all" mode.

---

## Flow Summary Matrix

| Flow | Roles Allowed | Entitlement | Audit Events |
|------|--------------|-------------|--------------|
| Onboarding | Admin (auto-assigned) | Trial | tenant_registered, connection_created |
| Table Explorer | All roles | Trial (CSV), Pro (Excel) | table_exported |
| Audit Viewer | Admin only | All plans | (read-only, no new events) |
| Connection Management | Admin | Trial: NS + 1 source; Pro: unlimited | connection_created, connection_tested, connection_revoked |
