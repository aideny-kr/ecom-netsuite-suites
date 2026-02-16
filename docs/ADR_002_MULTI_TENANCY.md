# ADR 002 -- Multi-Tenancy Strategy
_Date: 2026-02-16_

## Status

Accepted

## Context

The NetSuite Ecommerce Ops Suite is a multi-tenant SaaS platform serving ecommerce finance teams. Each tenant (organization) stores sensitive financial data: OAuth credentials, transaction records, reconciliation findings, and audit trails.

We need tenant isolation that:

1. **Prevents data leakage** between tenants at the database level (not just application logic).
2. **Scales to hundreds of tenants** without operational burden per tenant.
3. **Is enforceable as a security boundary**, not just a convention.
4. **Supports efficient querying** across all tenant data in a single table.
5. **Works with our async Python stack** (FastAPI + SQLAlchemy + asyncpg).

## Decision

**Use Row-Level Security (RLS) with `SET LOCAL app.current_tenant_id` per database session.**

Every multi-tenant table includes a `tenant_id UUID NOT NULL` column. PostgreSQL RLS policies filter all SELECT, INSERT, UPDATE, and DELETE operations to rows matching the current tenant context.

### Implementation

```sql
-- Enable RLS on every multi-tenant table
ALTER TABLE connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE connections FORCE ROW LEVEL SECURITY;

-- Single policy per table
CREATE POLICY tenant_isolation ON connections
    USING (tenant_id = current_setting('app.current_tenant_id')::uuid);
```

Application code sets the tenant context at the start of every database session:

```python
# API middleware (per-request)
await session.execute(
    text("SET LOCAL app.current_tenant_id = :tid"),
    {"tid": str(request.state.tenant_id)}
)

# Worker (per-task)
session.execute(
    text("SET LOCAL app.current_tenant_id = :tid"),
    {"tid": tenant_id}  # from task arguments
)
```

`SET LOCAL` scopes the setting to the current **transaction**, ensuring it is automatically cleared when the transaction ends. This prevents tenant context leakage between requests sharing a connection pool.

### Enforcement Layers

Tenant isolation is enforced at three layers:

| Layer | Mechanism | Purpose |
|-------|-----------|---------|
| **Database (L1)** | PostgreSQL RLS policies | Hard boundary -- even raw SQL cannot cross tenants |
| **Application (L2)** | Middleware sets `SET LOCAL` before any query | Ensures RLS context is always set |
| **API (L3)** | JWT contains `tenant_id`; middleware extracts and validates | Prevents tenant impersonation |

If the application layer fails to set tenant context (bug), RLS defaults to **denying all rows** because `current_setting('app.current_tenant_id')` returns an empty string that matches no UUID, resulting in zero rows returned rather than data leakage.

## Alternatives Considered

### Option A: Schema-per-Tenant

Each tenant gets a dedicated PostgreSQL schema (e.g., `tenant_abc123.connections`).

| Aspect | Assessment |
|--------|-----------|
| Isolation | Strong -- schemas are separate namespaces |
| Operational cost | **High** -- each new tenant requires DDL (CREATE SCHEMA, CREATE TABLE x17, migrations per schema) |
| Migration complexity | **Very high** -- Alembic must run against every schema |
| Connection pooling | Requires `SET search_path` per request; risk of leakage |
| Cross-tenant queries | Difficult (need to union across schemas) |
| Scaling | Hundreds of schemas create real operational burden |

**Rejected** because migration and operational complexity is too high for a small team.

### Option B: Database-per-Tenant

Each tenant gets a dedicated PostgreSQL database.

| Aspect | Assessment |
|--------|-----------|
| Isolation | Strongest -- completely separate databases |
| Operational cost | **Very high** -- provisioning, backups, monitoring per database |
| Migration complexity | **Very high** -- must apply to every database independently |
| Connection pooling | Separate pools per database; resource intensive |
| Cross-tenant queries | Not possible without federation |
| Scaling | Impractical beyond tens of tenants without automation |

**Rejected** because the operational burden is extreme for a v1 product with a small team.

### Option C: Application-Only Filtering (WHERE tenant_id = ?)

Add `tenant_id` to all queries at the application layer, with no database-level enforcement.

| Aspect | Assessment |
|--------|-----------|
| Isolation | Weak -- depends entirely on application correctness |
| Operational cost | Low |
| Migration complexity | Standard (single schema) |
| Risk | **High** -- a single missed WHERE clause leaks data |
| Auditability | Hard to prove isolation to security auditors |

**Rejected** because it does not provide a database-level security boundary. A single ORM query missing a filter would leak tenant data.

## Trade-Off Summary

| Criterion | RLS (chosen) | Schema-per-Tenant | Database-per-Tenant | App-Only |
|-----------|-------------|-------------------|---------------------|----------|
| Isolation strength | Strong (DB-enforced) | Strong | Strongest | Weak |
| Operational simplicity | High | Low | Very low | Highest |
| Migration simplicity | High | Very low | Very low | Highest |
| Connection pool efficiency | High | Medium | Low | High |
| Cross-tenant admin queries | Possible (bypass RLS as superuser) | Union across schemas | Not practical | Easy |
| Security audit confidence | High | High | Highest | Low |
| Scaling to 500+ tenants | Easy | Manageable | Difficult | Easy |

## Consequences

### Positive

- **Single schema, single migration path.** Alembic runs once against one schema. No per-tenant DDL.
- **Database-enforced isolation.** Even if application code has a bug, RLS prevents cross-tenant access.
- **Fail-closed default.** If tenant context is not set, queries return zero rows (not all rows).
- **Efficient connection pooling.** All tenants share one pool; `SET LOCAL` scopes to the transaction.
- **Simple operational model.** One database to back up, monitor, and maintain.
- **Admin flexibility.** Superuser/migration roles can bypass RLS when needed for system-level operations.

### Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Developer forgets to set tenant context | Middleware runs automatically before every handler; integration tests verify RLS behavior |
| `SET LOCAL` not used (plain `SET` would persist on connection) | Code review + linter rule to enforce `SET LOCAL` only |
| RLS performance overhead | Minimal with indexed `tenant_id` columns; benchmark confirms <1ms overhead per query |
| Superuser bypasses RLS | Application database role is NOT superuser; migration role is separate |
| Tenant context leakage in async pool | `SET LOCAL` scopes to transaction; connection is clean when returned to pool |
| Need to query across tenants (admin/analytics) | Use a separate connection without RLS (superuser role) for admin dashboards |

### Monitoring

- Log a warning if any database query executes without `app.current_tenant_id` being set.
- Integration tests: verify that Tenant A cannot see Tenant B data through any API endpoint.
- Periodic audit: run a query as the application role without setting tenant context and confirm zero rows returned.

## References

- PostgreSQL Row Level Security: https://www.postgresql.org/docs/current/ddl-rowsecurity.html
- `SET LOCAL` documentation: https://www.postgresql.org/docs/current/sql-set.html
- Citus Data multi-tenancy guide: https://docs.citusdata.com/en/stable/develop/migration_mt_schema.html
