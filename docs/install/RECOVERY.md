# Dedicated installation backup and recovery

This procedure recovers one previously isolated company installation. It does not
extract a company from shared staging or authorize a deployment. Keep the complete
operations record, data, secrets and recovery evidence outside this public repository.
Use [existing-company preparation](EXISTING-COMPANY.md) for initial extraction and
[runtime provisioning](DEDICATED-RUNTIME.md) for the application boundary.

## Recovery contract

The supported drill is a **clean-shutdown, whole-cluster physical backup**, paired
with the workspace and configuration captured during the same write freeze. Use
the exact PostgreSQL image digest, CPU architecture, data-directory layout and
extension versions. It is not a PostgreSQL major-version upgrade or downgrade.
An existing installation's actual version takes precedence over a compose example.

Recover into new, empty volumes on an internal network, with no published database
port, API, worker, collector, Beat or connector egress. Preserve the original
volumes. Only the recovery operator may access the destination. A physical recovery
retains the original cluster identifier and runtime binding; never use the
`adopt-existing` command as proof of isolation for that copy. Verify the separately
recorded destination host, container, volume and network identities instead.

A backup is not accepted until it has been decrypted, restored and verified. A
copy on the same host proves local recovery only. Host-loss recovery additionally
requires a verified encrypted copy in authorized private storage and an independently
recoverable decryption key. Record bucket/object generation, access restrictions,
retention and a successful download/restore. Do not infer those from a local file.

## Before the write freeze

The release operator owns the backup, restore evidence and go/no-go decision; the
company's authorized infrastructure administrator owns remote storage and key
access. Name both people in the private deployment record. Record:

- Source host, Docker project, database/volume, company UUID, database name and
  PostgreSQL system identifier; include every tablespace, WAL and external config
  location. This single-volume procedure refuses external tablespaces/WAL symlinks.
- Actual backend, frontend, worker, collector, Beat, PostgreSQL and Redis image
  IDs/digests, CPU architecture, source commits and Alembic revision. Retain the
  exact image artifacts, or verify that each pinned digest is still retrievable.
  Source code alone does not reproduce an image built with floating dependencies.
- Compose/reverse-proxy configuration, baked frontend API origin/build ID,
  volume mappings, file roots/owners/modes and secret references. Include legacy
  attachment roots if database records still refer to them.
- The retained encryption key **and version**, required provider credentials and
  separate operator/runtime authentication. A new encryption key cannot recover
  old ciphertext. Never print secret values or include them in CLI arguments.
- User/role and company configuration digests, schema inventory, deterministic
  complete row hashes/counts for every table, foreign-key checks, workflow/run and
  audit history, and checksums of every available workspace/attachment file.
  Record pre-existing missing files and revoked, unusable credentials separately.
- Running jobs, pending deliveries, approvals, remote mutation intents and
  uncertain outcomes. Restoring a database does not undo an external ERP write.

Choose a maintenance window with no migrations or other writers. Stop schedule
admission first. Let in-flight work finish within its supported deadline, or record
its uncertain state for reconciliation. Stop Beat, then workers/collectors, then
API and all remaining writers. Preserve broker/denylist state separately according
to its storage contract; do not treat Redis as disposable merely because the
database is backed up.

## Capture a consistent recovery set

1. While writers are stopped, collect the private baseline above. Use operator
   authority for complete inventory; runtime RLS would hide rows. Verify the exact
   intended company and cluster before any operator action.
2. Shut PostgreSQL down cleanly. Require container state `exited`, exit code zero,
   no live process using the volume, and `pg_controldata` reporting `shut down`.
   Merely blocking application connections is insufficient for a file copy.
3. Mount the complete stopped PostgreSQL volume read-only in a network-disabled
   helper using the exact database image. Refuse external symlinks and archive the
   whole directory, including WAL, transaction status and role catalogs. Preserve
   numeric owners/modes. Never copy just individual table directories.
4. Capture all workspace/artifact roots, deployment configuration and necessary
   secret material during the same freeze. Preserve company instructions byte for
   byte. Capture Redis persistence cleanly if the recovery design will retain it;
   otherwise stop here until session revocation and queued-work reconciliation
   have an explicit, reviewed recovery path.
5. Create a private manifest with component sizes and SHA256 checksums, freeze time,
   source snapshot time, exact versions and known source omissions. Encrypt the
   complete recovery set with an authenticated, established encryption tool.
   Keep its recovery key outside the bundle and independently recoverable.
6. Verify command exit statuses, hash the encrypted result, and copy it only to the
   authorized private backup destination. Verify remote object generation and
   checksum by download; test recovery using that downloaded object. Do not place
   archives, keys, source payloads or identity manifests in tickets or Actions.

For a local GnuPG drill, use a private GnuPG home, a randomly generated mode-0600
passphrase file in separate custody, `--batch --no-symkey-cache --pinentry-mode
loopback --passphrase-file /private/key --cipher-algo AES256 --symmetric`, and a
new output path. Never use `--ignore-mdc-error`. Keep shell tracing off, check every
pipeline member (`pipefail`), and retain sanitized failure evidence. The drill's
passphrase file on the same host is not independent disaster-recovery custody.

## Restore and verify before enabling work

1. Verify the encrypted object's checksum against the independently retained
   backup record. Decrypt to a new private staging file and require successful
   authentication/exit **before extracting anything**. Check a wrong key and a
   damaged object are rejected. Delete any partial failed decryption output.
2. Inspect archive entries and component checksums. Refuse path traversal, links
   outside approved roots, duplicate/unexpected files or a missing manifest. Mount
   the archive read-only and extract only into newly created, verified-empty
   task-owned volumes; never overlay an existing installation.
3. Start PostgreSQL alone with the exact archived digest and architecture on the
   isolated network. Confirm the version, extensions, schema, database/company
   identity and preserved cluster identifier. Never silently start a newer/older
   major version against these data files.
4. Recompute all table hashes/counts, original identities/password hashes/role
   memberships, company configuration, jobs/runs, evidence and audit history;
   verify every foreign key. Compare workspace and attachment checksums with the
   capture manifest, including unchanged instructions. Distinguish known source
   omissions from newly lost data.
5. Decrypt stored connection/AI credentials in memory using the recovered key;
   emit counts only. Compare pre-existing revoked failures individually using
   private ciphertext/record hashes. Do not refresh OAuth tokens or contact a
   provider to test decryptability. Never reset passwords or keys to force a pass.
6. Connect using the **actual restricted runtime login**, run
   `validate_runtime_database`, verify expected company reads, negative tenant
   context, read-only shared authorization catalogs and append-only audit. Check
   effective user permissions and preserve disabled historical identities.
   Before any provisioning transaction, verify zero transaction sampling and
   reviewed logging extensions as described in the runtime release record.
7. Validate model/schema compatibility and restore the exact pinned application
   images/configuration. Test API health, safe authentication/refresh/permission
   checks and workspace accessibility in isolation. First keep background
   consumers stopped. Do not dispatch restored schedules or approvals.
8. Reconcile every in-flight/uncertain action against authoritative remote evidence
   before any resend; preserve idempotency keys and side-effect logs. Stale broker
   messages must not silently replay. Retain the Redis denylist or explicitly
   invalidate old access/refresh sessions through the supported auth mechanism.
   Verify recovery of both business data and authentication revocation state.
9. Only after the separate release/cutover gate passes, start API, verify it, then
   workers/collectors, and Beat last. Check all running image IDs and migration
   head. Restore ingress only to the approved target and run its safe smoke tests.
   Keep the failed/original installation fenced to prevent two active writers.

## Upgrade and rollback decision

Every release records its exact prior and candidate revisions/images, schema
compatibility and tested recovery set before migrations. A code-only rollback is
allowed only when the prior image is proven compatible with the current schema
and persisted data. Pin every affected service, including the frontend; an API
health response alone cannot prove that workers or frontend rolled back.

For an incompatible or irreversible migration, retain the failed database for
analysis and restore the **whole matched pre-upgrade set** into fresh volumes,
then verify it as above. Account explicitly for writes and external effects since
the freeze. Forward repair may be safer than losing newer transactions. Never run
an unreviewed `alembic downgrade` against a company database, delete newer evidence,
or repost an ERP transaction to make restored history look complete.

The legacy `.github/workflows/rollback.yml` targets the legacy staging/production
compose deployment and accepts an optional schema downgrade. It is **not** the
dedicated-installation recovery entry point: it does not restore this matched
database/files/keys set or establish major-version/schema compatibility. Do not
dispatch it for a dedicated recovery merely because it is named Rollback.

## Evidence and recovery objectives

Record capture/encryption, transfer, restore and verification durations separately.
RPO is bounded by the last verified source capture, not the later encryption date.
RTO includes retrieving images and keys, restoring files/database, verification,
external-outcome reconciliation and authorized traffic restoration. A local drill
duration excludes cloud download, human access delays and target cutover; do not
present it as a production SLA.

For launch, perform a verified recovery set immediately before each migration or
cutover, with writers frozen. Set a recurring cadence, retention, host-loss RPO/RTO
and named second operator in the private operational plan before live activation.
Those service commitments require actual infrastructure and owner agreement.
Failed/partial backups cannot advance the last-successful-backup timestamp.

The public evidence summary should contain only reviewed revisions, method,
aggregate verification results, actual limitations and remaining gates. Keep
locations and detailed record/file digests private. Clean up task-owned temporary
plaintext and containers after verified encryption/recovery, while retaining
approved recovery objects and keys under the recorded custody policy. Never
remove original customer volumes or prior acceptance evidence as drill cleanup.

Reference: PostgreSQL's [file-level backup requirements](https://www.postgresql.org/docs/17/backup-file.html)
explain the clean shutdown and whole-cluster boundary; its [logical backup guide](https://www.postgresql.org/docs/17/backup-dump.html)
distinguishes portable dumps from version-specific physical copies. GnuPG's
[operational commands](https://www.gnupg.org/documentation/manuals/gnupg/Operational-GPG-Commands.html)
describe symmetric encryption and decryption.
