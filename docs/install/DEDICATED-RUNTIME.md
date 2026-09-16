# Dedicated runtime access

Use `compose.dedicated.yml` for the supported private-Postgres deployment, behind a
separately managed HTTPS reverse proxy. `compose.single-company.yml` remains a
**development evaluation** profile; its operator connection is not a production
runtime. The dedicated profile does not perform migrations or company creation.
Framework cutover, source reconciliation, backup/restore and recovery acceptance
remain separate release gates. Do not point this installer at shared staging.

## Authority and storage

Use one isolated PostgreSQL cluster for this installation. Retain the adopted
company's encryption key and its version with the encrypted database backup; a
new key cannot decrypt existing connection secrets. Keep custody and recovery
locations in the private operations record, never in this public repository or
ClickUp attachments. Generate a separate JWT signing key for this environment;
sessions from another environment should not authenticate here. Preserve existing
provider keys or configure explicitly authorized deployment-specific keys.

Create private directories outside every checkout/build context. Runtime reads
named files in `/run/secrets` through Settings (environment values take precedence):

- `DATABASE_URL`: asyncpg URL for `suite_runtime` at `postgres:5432/suite_studio`.
- `DATABASE_URL_SYNC`: matching synchronous PostgreSQL URL for worker audit writes.
- `JWT_SECRET_KEY`: at least 48 random characters; `ENCRYPTION_KEY`: retained Fernet key.
- `ENCRYPTION_KEY_VERSION` when applicable; `EMAIL_API_KEY` and `EMAIL_FROM_ADDRESS`
  for the authorized Resend sender. Console invitation output is forbidden.
- Only the provider keys actually authorized for this installation, using their
  Settings names, such as `ANTHROPIC_API_KEY` or `OPENAI_EMBEDDING_API_KEY`.

Use the non-root production image (`backend/Dockerfile.prod`). Runtime secret
directories/files must be owned by that image's `appuser` numeric UID/GID, with
directory mode 0500/0700 and file mode 0400/0600; verify readability as that user.
Do not make secrets world-readable or run runtime as root to fix a mount error.
Workspace storage must likewise be writable by that UID/GID. Operator credentials
have separate operator-only ownership. Restrict host and Docker-daemon access.
API/worker/Beat receive this runtime directory only. They must never receive
`DATABASE_URL_DIRECT`, `DATABASE_URL_DIRECT_SYNC`, the Postgres operator password,
`RUNTIME_OPERATOR_DATABASE_URL`, or backup keys. No database/Redis ports are
published. PostgreSQL and Redis occupy an internal network; runtime services also
have an outbound network for authorized connectors. API/frontend ports bind only
to loopback for the TLS proxy. No secret is a build argument.

The separate operator environment holds migration/bootstrap database authority.
Use it only in a short-lived process; never a worker, Beat or API container. Supply
its variables through a private environment file or secret-manager launcher. Do
not put a connection URL/password in shell arguments, logs or a PR body.

## Ordered installation and upgrades

1. Pin reviewed backend/frontend/pgvector/Redis image digests. Configure the exact
   HTTPS `PUBLIC_ORIGIN` and same-origin NetSuite callback
   `/api/v1/connections/netsuite/callback`. The frontend image must have been built
   with `NEXT_PUBLIC_API_URL` matching this API origin,
   `NEXT_PUBLIC_SINGLE_COMPANY=true`, and `NEXT_PUBLIC_BUILD_ID` identifying the
   reviewed revision. Verify these baked values before starting it. The reverse proxy
   forwards `/api/` to backend and other routes to frontend, preserving streaming.
2. Create or restore the explicitly chosen database/workspace volumes. Set
   `DATABASE_VOLUME`, `WORKSPACE_VOLUME`, `POSTGRES_PASSWORD_FILE` and
   `RUNTIME_SECRETS_DIR` to the private deployment's verified resources. The compose
   file requires existing external volumes and never deletes their contents.
3. With all runtime services stopped, run Alembic using the operator connection.
   Bootstrap a fresh synthetic/company database, or follow `EXISTING-COMPANY.md`
   for an already-isolated, verified adoption. Provisioning never extracts or deletes
   tenants and never edits soul files, model selections or financial approvals.
4. Seed/review shared documentation and metrics with operator authority using the
   existing seeders. Runtime can read shared catalogs but cannot rewrite them.
   Their automatic reseed tasks are excluded from dedicated Beat and safely skip
   if called directly. Refresh these catalogs as part of an operator release.
5. Privately generate a 32+ character URL-safe runtime password once. Record the
   target database, cluster `system_identifier`, and adopted company UUID. Use the
   reviewed image with the operator's `RUNTIME_OPERATOR_DATABASE_URL` environment
   variable and a mode-0600 password file mounted privately. Preview:

   ```sh
   python -m app.cli.runtime_role --database suite_studio \
     --cluster "$EXPECTED_CLUSTER" --company "$EXPECTED_COMPANY" \
     --password-file /private/runtime-password
   ```

   Review its classification/result, then repeat with `--apply`. The operation is
   atomic. It refuses mismatched identities, foreign tenant rows, unclassified
   global tables, existing unmanaged roles, elevated memberships/ownership and
   unreviewed SECURITY DEFINER functions. A repeat preserves the installed role's
   password and company binding; this is not a password-rotation command.
6. Put that runtime credential in the two runtime URL secret files. Start the
   dedicated compose profile and verify API, worker, collector and Beat startup.
   Startup checks the actual login role, database binding, tenant context, table
   policy coverage, read-only metadata and forbidden privileges. A newly migrated
   table blocks startup until the operator reviews/reapplies provisioning.
7. Verify image IDs/digests, real user/permission flows, a bounded synthetic
   scheduled job and negative cross-company/permission checks. No live financial
   write is authorized by this procedure. Record exact revision and evidence.

Reserve a maintenance window: provisioning takes table locks and fails quickly
when an active transaction holds them. Every upgrade follows the same stop → backup → migrate → seed → provision →
verify → start order. Never grant runtime `BYPASSRLS`, table ownership, schema
creation, membership in operator roles, or write access to global permissions to
make a failing startup pass. An error is a release blocker to investigate.

## Database guarantees and limits

The `suite_runtime` login is non-owner, non-superuser, no-inherit, no-createdb,
no-createrole, no-replication and no-bypassrls. Its default tenant context survives
transaction commits. Existing RLS policies remain; missing tenant policies are
added. A restrictive policy pins every writable scoped table to the installed
company independently of the session's freely settable tenant GUC. Changing that
GUC can deny access but cannot reveal another company. Cursor state is bounded
through its connection. Global roles, permissions, schema version and curated
knowledge are read-only. BigQuery discovery may replace only schema chunks in
`bi/schema-docs/<installed-company-uuid>/`; database policies enforce that namespace
and preserve curated rules. Legacy unscoped BigQuery chunks remain intact but are
hidden from runtime until authorized re-discovery creates scoped replacements.
Shared SYSTEM document/metric rows are readable, but
INSERT/UPDATE/DELETE and moving their ownership are blocked. Audit is append-only. Retention/deletion requires a separately authorized operator
maintenance process; do not dispatch audit-retention tasks with runtime authority.

This is a **company** database boundary. Existing application permissions still
separate users/tools within the company; runtime database credentials are not
end-user credentials. Operator/database-host authority can change the boundary
and therefore remains separately controlled. This profile deliberately supports
private Postgres only; it does not inherit the legacy Supabase TLS shortcut.

The local verification is synthetic. A passing boot does not establish live DNS,
TLS, provider credentials, backup recovery or cutover completion. Keep those
acceptance checks in their deployment/recovery tickets and the private runbook.
