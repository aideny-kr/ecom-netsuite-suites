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
environment. `--expected-database` is the exact database name, **not a DSN**.
The UUID and slug must match the restored company. An empty, shared, inactive,
misidentified or administrator-less company database is rejected.

```sh
SINGLE_COMPANY=true python -m app.cli.company adopt-existing \
  --expected-database company_rehearsal \
  --tenant-id 11111111-1111-1111-1111-111111111111 \
  --tenant-slug example-company
```

This is a preview by default. After reviewing the target and preservation evidence,
repeat with `--apply`. The operator command needs table-lock and complete inventory
visibility; never grant those powers to the application runtime just to run it.
It refuses a database whose name or company identity differs, and fails if RLS
would hide company records from the operator.

The applied transaction changes only the selected company's plan to `self_hosted`
and clears its commercial expiry, with the normal updated timestamp and one audit
event. It does not grant roles, turn on feature flags, activate schedules, change
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
