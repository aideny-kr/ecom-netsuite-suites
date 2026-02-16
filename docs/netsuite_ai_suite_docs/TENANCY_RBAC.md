# Tenancy & RBAC
_Last updated: 2026-02-15_

## Tenancy model (recommended v1)
Single Postgres database with:
- `tenant_id` on every multi-tenant table
- Postgres Row Level Security (RLS) policies enforcing tenant isolation

## Identity & roles
Actors:
- user (human)
- service_account (automation / connector jobs)

Roles (v1):
- Admin: manage connections, users, entitlements, approvals, exports
- Finance: reconciliation runs, evidence packs, scheduling
- Ops: table views, monitoring, limited exports
- Read-only: view tables only

## Enforcement
- Server-side authorization middleware
- DB-level RLS as a second layer
- Every privileged action emits an audit event
