# MCP Tool Governance Checklist
_Last updated: 2026-02-16_

This document defines the governance rules, validation requirements, and audit obligations for every MCP tool in the system. All tools must pass this checklist before deployment.

---

## Governance Principles

1. **Tools are the only way the AI model acts.** No direct database access, no raw API calls.
2. **Every tool invocation is audited.** No silent operations.
3. **Read tools have row limits.** No unbounded queries.
4. **Write tools require approval.** No autonomous financial mutations.
5. **All parameters are validated.** No injection, no bypass.

---

## Per-Tool Governance Configuration

### Tool: `run_suiteql`

| Parameter | Value |
|-----------|-------|
| **Description** | Execute a SuiteQL query against NetSuite |
| **Default LIMIT** | 100 rows |
| **Max Rows** | 1,000 rows |
| **Allowlisted Tables** | `transaction`, `transactionline`, `account`, `customer`, `item`, `subsidiary`, `department`, `classification`, `location`, `employee`, `vendor`, `currency`, `accountingperiod` |
| **Denylisted Tables** | `loginaudit`, `systemnote` (sensitive system tables) |
| **Timeout** | 30 seconds |
| **Rate Limit** | 20 calls per minute per tenant |
| **Requires Approval** | No (read-only) |
| **Entitlement** | Trial: 50 calls/day; Pro: configurable (default unlimited) |

**Validation rules:**
- Query must be a valid SuiteQL SELECT statement (no INSERT, UPDATE, DELETE, DROP, ALTER).
- Query must include a LIMIT clause; if missing, tool injects `LIMIT {default_limit}`.
- LIMIT value must not exceed `max_rows`.
- All referenced tables must be in the allowlist.
- Query must not contain subqueries referencing denylisted tables.
- Query must not contain comments (`--`, `/* */`) to prevent injection via comment-based bypass.

### Tool: `list_tables`

| Parameter | Value |
|-----------|-------|
| **Description** | List available canonical tables and their schemas |
| **Default LIMIT** | N/A |
| **Max Rows** | N/A |
| **Allowlisted Tables** | All canonical tables |
| **Timeout** | 5 seconds |
| **Rate Limit** | 60 calls per minute per tenant |
| **Requires Approval** | No |
| **Entitlement** | All plans |

**Validation rules:**
- No parameters required.
- Returns table names and column schemas only (no data).

### Tool: `query_table`

| Parameter | Value |
|-----------|-------|
| **Description** | Query a canonical table with filters, sorting, and pagination |
| **Default LIMIT** | 50 rows |
| **Max Rows** | 500 rows |
| **Allowlisted Tables** | `canonical_orders`, `canonical_payouts`, `canonical_refunds`, `canonical_fees`, `canonical_disputes`, `canonical_ns_transactions` |
| **Timeout** | 15 seconds |
| **Rate Limit** | 30 calls per minute per tenant |
| **Requires Approval** | No (read-only) |
| **Entitlement** | All plans |

**Validation rules:**
- `table` parameter must be in the allowlist.
- `filters` must use allowed operators: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `like`.
- `sort_by` must be a valid column name for the specified table.
- `limit` must not exceed `max_rows`.
- No raw SQL injection via filter values (parameterized queries only).

### Tool: `export_table`

| Parameter | Value |
|-----------|-------|
| **Description** | Export a canonical table to CSV or Excel |
| **Default LIMIT** | 10,000 rows |
| **Max Rows** | 50,000 rows |
| **Allowlisted Tables** | Same as `query_table` |
| **Timeout** | 60 seconds |
| **Rate Limit** | 5 calls per minute per tenant |
| **Requires Approval** | No |
| **Entitlement** | Trial: CSV only, 1,000 row limit; Pro: CSV + Excel, 50,000 row limit |

**Validation rules:**
- `format` must be `csv` or `xlsx`.
- Trial plan: reject `xlsx` format; enforce 1,000 row limit.
- `table` must be in the allowlist.

### Tool: `get_job_status`

| Parameter | Value |
|-----------|-------|
| **Description** | Get the status and result of a background job |
| **Default LIMIT** | N/A |
| **Max Rows** | N/A |
| **Timeout** | 5 seconds |
| **Rate Limit** | 60 calls per minute per tenant |
| **Requires Approval** | No |
| **Entitlement** | All plans |

**Validation rules:**
- `job_id` must be a valid UUID.
- Result filtered by RLS (tenant can only see own jobs).

### Tool: `create_journal_entry` (Stub -- Phase 2+)

| Parameter | Value |
|-----------|-------|
| **Description** | Create a journal entry in NetSuite |
| **Default LIMIT** | N/A |
| **Max Rows** | N/A |
| **Timeout** | 60 seconds |
| **Rate Limit** | 5 calls per minute per tenant |
| **Requires Approval** | **Yes** (human-in-the-loop) |
| **Entitlement** | Pro only; writeback feature flag must be enabled |

**Validation rules:**
- Requires explicit approval token from an authorized user.
- Idempotency key required in parameters.
- Entry must reference valid accounts from tenant config.
- Line count must not exceed `posting_batch_size`.
- Phase 1: returns stub response with governance metadata.

---

## Approval Requirements for Write Operations

### Approval Flow

```
AI proposes action
  |
  v
Tool checks requires_approval = true
  |
  v
Tool returns {status: "pending_approval", action_summary, parameters}
  |
  v
User reviews in UI and clicks Approve/Deny
  |
  +-- Approve --> Tool executes with approval_token
  |                 |
  |                 v
  |              Audit: tool_executed {approval_token, approver_id}
  |
  +-- Deny ----> Audit: tool_denied {denier_id, reason}
```

### Approval Rules

| Rule | Detail |
|------|--------|
| Write tools always require approval | `create_journal_entry`, `post_adjustment`, `update_record` |
| Approval token is single-use | Cannot be replayed |
| Approval expires | 15-minute TTL from issuance |
| Approver must have `admin` or `finance` role | `readonly` and `ops` cannot approve |
| Approval is logged | `category='mcp_tool'`, `action='tool_approved'` or `tool_denied'` |

---

## Audit Fields Logged Per Tool Call

Every MCP tool invocation creates an `audit_events` row with the following data:

| Audit Field | Source | Example |
|------------|--------|---------|
| `tenant_id` | Request context | `550e8400-...` |
| `actor_id` | JWT / caller identity | `7c9e6679-...` |
| `actor_type` | JWT claim | `user` |
| `category` | Fixed | `mcp_tool` |
| `action` | Tool lifecycle | `tool_invoked`, `tool_completed`, `tool_failed`, `tool_denied` |
| `resource_type` | Tool name | `run_suiteql` |
| `resource_id` | Tool call ID | `call_abc123` |
| `correlation_id` | Propagated from request | `req_xyz789` |
| `status` | Outcome | `success`, `error`, `denied` |
| `payload` | Tool-specific (see below) | JSON object |

### Payload Structure by Action

**`tool_invoked`:**
```json
{
  "tool": "run_suiteql",
  "parameters": {
    "query": "SELECT id, tranid FROM transaction WHERE type = 'Deposit' LIMIT 100",
    "limit": 100
  },
  "governance": {
    "applied_limit": 100,
    "timeout_seconds": 30,
    "requires_approval": false
  }
}
```

**`tool_completed`:**
```json
{
  "tool": "run_suiteql",
  "rows_returned": 42,
  "execution_time_ms": 1230,
  "truncated": false
}
```

**`tool_failed`:**
```json
{
  "tool": "run_suiteql",
  "error_type": "timeout",
  "error_message": "Query exceeded 30 second timeout"
}
```

**`tool_denied`:**
```json
{
  "tool": "run_suiteql",
  "denial_reason": "table_not_allowlisted",
  "denied_table": "loginaudit",
  "denial_details": "Table 'loginaudit' is not in the SuiteQL allowlist"
}
```

---

## Parameter Validation Rules

### General Rules (All Tools)

| Rule | Implementation |
|------|---------------|
| Type checking | All parameters validated against JSON Schema before execution |
| Required parameters | Missing required params return 422 with field-level errors |
| String length limits | All string parameters have max length (e.g., query: 10,000 chars) |
| UUID validation | All ID parameters must be valid UUID v4 format |
| Enum validation | Enum parameters must match allowed values exactly |
| No null bytes | String parameters must not contain `\x00` |
| Encoding | All strings must be valid UTF-8 |

### SQL Injection Prevention (SuiteQL Tools)

| Rule | Implementation |
|------|---------------|
| Statement type | Only SELECT allowed; reject INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/TRUNCATE |
| Keyword blocklist | Reject queries containing: `INTO OUTFILE`, `LOAD_FILE`, `SLEEP`, `BENCHMARK`, `WAITFOR` |
| Comment stripping | Reject queries containing `--` or `/* */` |
| Table validation | Parse table references and validate against allowlist |
| LIMIT enforcement | Inject or cap LIMIT clause |
| Parameterization | Where possible, use parameterized queries for filter values |

---

## Rate Limiting

### Per-Tenant Limits

| Tier | Window | Max Calls (all tools combined) |
|------|--------|-------------------------------|
| Trial | 1 minute | 30 |
| Trial | 1 day | 200 |
| Pro | 1 minute | 120 |
| Pro | 1 day | 5,000 |

### Per-Tool Limits

Each tool has its own per-minute rate limit (defined in tool configuration above). The per-tool limit is enforced in addition to the per-tenant aggregate limit.

### Rate Limit Response

```json
{
  "error": "rate_limit_exceeded",
  "tool": "run_suiteql",
  "limit": 20,
  "window": "1 minute",
  "retry_after_seconds": 35
}
```

Rate limit hits are logged: `category='mcp_tool'`, `action='rate_limited'`.

---

## Pre-Deployment Checklist

For each tool before it goes live, verify:

- [ ] Tool definition includes all governance fields (default_limit, max_rows, timeout, rate_limit, requires_approval)
- [ ] Parameter JSON Schema is defined and validated
- [ ] Allowlisted tables/resources are explicitly enumerated
- [ ] Denylisted tables/resources are explicitly enumerated
- [ ] Rate limit is configured per tool and per tenant tier
- [ ] Entitlement check is implemented (trial vs pro)
- [ ] Audit event is emitted on invocation (with full payload)
- [ ] Audit event is emitted on completion (with row counts and timing)
- [ ] Audit event is emitted on failure (with error details)
- [ ] Audit event is emitted on denial (with denial reason)
- [ ] Timeout is enforced (query/request cancelled after limit)
- [ ] Write tools require approval token
- [ ] Approval token is validated (single-use, not expired, authorized role)
- [ ] SQL injection prevention (for query-based tools)
- [ ] Response does not contain credentials or sensitive fields
- [ ] Integration test covers governance enforcement
- [ ] Load test confirms rate limiting works under concurrent requests
