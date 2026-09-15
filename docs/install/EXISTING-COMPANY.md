# Prepare an existing company installation

Fresh `bootstrap` does not migrate a hosted company. Existing companies keep their
UUID, users, role memberships, configuration, encrypted connections, instructions,
schedules, approvals, evidence, audit history and workspace files.

## Isolate before conversion

Inventory the source using a read-only database transaction. Record the schema
revision, company UUID/slug, table counts and relationships privately. A shared
database must use a reviewed, company-scoped export/import into a separate empty
database. **Never delete unrelated companies to satisfy the single-company check.**

The export/import and its preservation rehearsal are separate release gates.
The adoption command below does not implement extraction, restore, key rotation,
workspace copying or deployment. Keep the destination offline: no API, worker,
Beat, provider credentials or external network access while rehearsing.

The export manifest must classify every source table. Copy company-scoped rows
using the explicit UUID; preserve the system catalog owner and required shared
role/permission identities. Include cursor state only through selected connection
IDs. Review shared knowledge independently rather than copying unrelated content.
Retain foreign keys; missing cross-table references block acceptance. Preserve
encrypted bytes and arrange authorized key custody separately without putting
keys in the export manifest. Copy tenant workspace files with a private checksum
manifest, including company instructions; never regenerate them from defaults.

Use a consistent source snapshot. Record row counts and deterministic content
digests per table, primary/foreign-key identities, effective user permissions and
workspace checksums before/after restore. A live source can change after the
snapshot: the eventual cutover needs a write freeze or reviewed final delta plan.
Neither an old snapshot nor a successful preview authorizes production cutover.

## Preview the isolated destination

Run with the operator/migration database connection selected privately through the
environment. **`DATABASE_URL_DIRECT` takes precedence over `DATABASE_URL`**, including
values inherited from the environment or repository-root `.env`. Explicitly set
both to the reviewed destination connection. The command prints the selected
variable name, never its value. `--expected-database` is the exact database name, **not a DSN**.
Require the destination's `system_identifier` from `SELECT system_identifier FROM
pg_control_system()` as `--expected-system-identifier`. Obtain this identifier from
the independently verified destination provisioning/restore record, not by asking
an unverified application connection what value would make its check pass. This
distinguishes independently initialized clusters, including logical restores that
retain the same database name and company identities. Physical copies (PITR,
pg_basebackup, disk/volume clones and replicas) retain the system identifier.
**This adoption procedure supports logical restores into independently initialized
clusters only.** Physical-clone adoption needs a separately verified restore marker
before it can be supported; the cluster check alone cannot distinguish those copies.
The check requires operator access to that function.
The UUID and slug must match the restored company. An empty, shared, inactive,
misidentified or administrator-less company database is rejected.

```sh
SINGLE_COMPANY=true python -m app.cli.company adopt-existing \
  --expected-database company_rehearsal \
  --expected-system-identifier 1234567890123456789 \
  --tenant-id 11111111-1111-1111-1111-111111111111 \
  --tenant-slug example-company
```

This is a preview by default. After reviewing the target and preservation evidence,
repeat with `--apply`. The operator command needs table-lock and complete inventory
visibility; never grant those powers to the application runtime just to run it.
It refuses a different cluster, database or company identity. It also refuses rows
for another company in any public table with a tenant_id column, including orphan
rows whose company has no tenants record. Catalog-based discovery includes tables
the operator cannot read; missing permissions and RLS-filtered reads fail closed.
Locks keep those tables stable through conversion, with a five-second lock timeout.
Complete permission/relationship and shared-row verification still belongs to the
export/import rehearsal. After a failed apply, rerun the preview before retrying;
connection loss during commit can leave an uncertain stored outcome.

The applied transaction changes only the selected company's plan to `self_hosted`
and clears its commercial expiry, with the normal updated timestamp and one audit
event. Commercial entitlements do change: formerly plan-restricted capabilities
(for example chat API and policy access) and commercial quotas follow the
`self_hosted` plan. The preview shows each changed limit/capability and the audit
records that difference and the verified cluster identity. Existing role and feature
gates still apply. It does not grant roles, turn on feature flags, activate schedules, change
budgets, reset credentials, approve actions or modify company instructions.
Reapplying the same conversion adds no further changes or audit events. Audit
failure rolls back the plan change. Concurrent bootstrap/adoption calls serialize.

## Acceptance before runtime or cutover

- Verify restored row identities, relationships, counts/digests and effective
  permissions, plus unchanged encrypted credentials and workspace checksums.
- Demonstrate existing admin login and invited-member acceptance; public company
  creation and platform-superadmin operations must remain disabled.
- Demonstrate preview, replay, wrong-target rejection and failed-audit rollback.
- Run required CI, seeded-company end-to-end checks and independent T2 review at
  the exact revision. Complete least-privilege runtime and backup/restore gates.
- Record the authorized target, deployment, live smoke and rollback separately.

Synthetic regression coverage is useful but does not prove preservation of a real
company export. Keep the migration ticket open until that rehearsal is verified.

## Selective snapshot utility

`python -m app.cli.company_snapshot` exports a reviewed policy and restores it into
an **empty offline rehearsal database**. It requires `SNAPSHOT_DATABASE_URL`
explicitly and never falls back to the application's environment configuration.
The restore CLI accepts only loopback destinations whose names end in `_rehearsal`.
Keep database and filesystem archives in a private directory outside the repository.

The private JSON policy has exactly `version` (1), `tenant_id`, `tenant_slug`,
`schema_revision` (list), and `tables`. Every public table needs an explicit rule:

- `tenant`: exact company rows; optional `shared_ids` admits only reviewed system
  or NULL-owner rows with those UUIDs.
- `company`: only `tenants`, selecting the company and system owner.
- `connections`: only `cursor_states`, selected through the company's connections.
- `all`: only `roles`, `permissions`, `role_permissions`, and `alembic_version`.
- `ids`: only `domain_knowledge_chunks`, with an explicit reviewed UUID list.

A changed table inventory, absent reviewed ID or mismatched schema revision stops
export. The utility holds one repeatable-read, read-only transaction and retains
primary keys, exact encrypted bytes, JSON and numeric values through PostgreSQL
binary COPY. The archive carries completion records, per-table counts and SHA256
checksums. Partial archives cannot be imported. Stored generated columns are
recomputed and their complete row contents are checked against the source too.

Provision a fresh independent PostgreSQL cluster with the same major version and
matching supported extensions. Load a reviewed **schema-only** copy, without seed
rows, into the destination. Compare actual source schema drift with migrations;
never drop source-only columns to make a restore pass. Columns, defaults, primary
and foreign keys, indexes, triggers, policies and application functions must match.
The supported application extensions are plpgsql, vector, pgcrypto, uuid-ossp,
pg_trgm and btree_gin. Managed platform schemas/extensions are outside this public
application snapshot; separately review any application dependency on them. Views,
materialized views, sequences, identity columns, virtual generated columns and
custom binary types other than vector require an explicit design before support.

Export with `--policy`, `--archive` and `--report`, each pointing to a private path.
All output paths must be new. Review the completed archive's SHA256 separately.
Restore uses those same arguments plus `--expected-sha256`, `--expected-database`
and `--expected-cluster` from the independently verified provisioning record.
No API, worker or scheduler should run against the destination. Isolate its network.

Restore owns one transaction and locks every table. It refuses a populated target
or a source/destination cluster match. Operator-only trigger suppression permits
exact import and circular references; it is local to the transaction. Before commit,
all declared foreign keys (including composite/MATCH FULL), row scope, counts and
content checksums are verified. Triggers are restored before commit. RLS-filtered
operator reads fail closed. A rerun refuses existing rows instead of replacing them.
If a connection/report failure leaves the outcome uncertain, inspect the destination;
do not delete rows and retry against it. Use a newly provisioned target when needed.

This utility does not copy workspace files, transfer decryption keys, grant runtime
privileges, activate schedules or authorize cutover. Verify those separately with
private before/after checksums and the adoption acceptance checks above.

### Export window and schema fidelity

Use a window with no deployments, migrations, table rewrites or TRUNCATE operations.
Ordinary application writes may continue under the consistent snapshot. Export
acquires all table locks before its first snapshot query; this avoids PostgreSQL's
[table-rewrite snapshot caveat](https://www.postgresql.org/docs/17/mvcc-caveats.html).
Locks remain held until export finishes, so a queued schema change can delay normal
traffic behind it. Source lock timeout is five seconds; statement and idle-transaction
timeouts are explicitly fifteen minutes. An interrupted export must start again.

Database encoding, locale/provider/version and column collations must also match.
Collatable keys use C ordering for deterministic checksums. Only the explicit
portable builtin type allowlist (and their arrays) plus vector is supported;
cluster-local identifiers such as oid/regclass are refused. Public rules and
ALWAYS/REPLICA triggers are refused because replica mode would not suppress them.
The same one-MiB frame limit is enforced during export and restore.

A schema dump can reparse equivalent CHECK/index expressions into a different
catalog representation. Preserve the strict comparison: provision through native
migrations and reviewed source drift when necessary. In a freshly created,
independently initialized, disposable destination only, remove that provisioning
run's generated bootstrap catalogs after verifying their exact initial contents
and that all other tables are empty. Never clear existing company data, and never
prune a shared source. Record this preparation separately from the logical import.
