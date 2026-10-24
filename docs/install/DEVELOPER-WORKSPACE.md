# Developer workspace checks and deployment recovery

Manual edits save review drafts. Reopen the changeset to inspect its diff, then
use the existing approval and apply controls. Saving a draft does not deploy it.

Sandbox deployment requires current validation and test receipts for the exact
project content. Validation must also identify the destination account and numeric
role. Preview signs and persists both values; confirmation rejects changed
validation identity, and the worker rejects an adapter returning a different role.
Migration `103_workspace_target_identity` adds this binding. Existing previews
must be recreated after upgrade; the next preview supersedes an unbound token
without waiting for expiry. No identity is inferred for an old token. Older
receipts without this evidence are stale: rerun both checks. Approval and the
preview confirmation remain separate requirements. Production is disabled.

SuiteCloud validation and deployment are currently **unavailable**. Oracle CLI
3.2 requires certificate-based SDK authorization and a project `defaultAuthId`;
the application's REST OAuth connection does not provide either. The runner
fails closed before launching the CLI or refreshing REST tokens. It does not
write a guessed credential file or use credentials from the host. Existing
synthetic check/dispatch tests are not proof of a supported target deployment.

The certificate adapter remains unfinished. An internal authentication primitive
now isolates a digest-pinned SDK, Java home, private key and passkey, and checks
the returned account and numeric role. It runs without workspace hooks or project
configuration. It has no production caller or deploy operation; the seeder still
rejects all runs. Offline empty-store and synthetic protocol tests do not prove
an authorized certificate handshake.

Activation also requires a separate credential host or container that cannot
co-host or be inspected by tenant-authored workspace commands. Mode 0700/0600
files do not isolate processes sharing an OS user. JVM attach/performance data
are disabled, but these settings do not replace that isolation boundary. The
existing Jest runner must not share this credential context. Production seeding
stays closed until the execution boundary is implemented and reviewed.

Remaining work includes binding the current connection and certificate identity
through validation, preview, confirmation and worker execution, a trusted project
execution path, and actual authorized sandbox acceptance. A role display name
alone is insufficient identity evidence. Configuring a certificate or
selecting a sandbox requires the installation's existing authorization; this
document does not grant it.

Activation must use matching reviewed API and worker images. An older worker
does not enforce the new identity pin; the always-closed seeder remains required
throughout any rollout until the complete target path is accepted.

A confirmed deployment is saved before delivery to the worker. If broker
delivery fails, its run remains **queued**. Expand the run and use **Retry
delivery** to send that same confirmed run. The original actor must still have
permission. The server also rechecks permissions, company activation, current
content, manifest, destination and required checks before executing it.

Once the worker claims a deployment, it durably becomes **running**. Repeated
delivery cannot execute it again. A crash or lost acknowledgment can leave an
uncertain external outcome: inspect the target and reconcile the saved run.
There is no automatic replay or reset for running/terminal deployments. Never
create another deployment merely because an acknowledgment was lost.

For rollout, stop old deployment consumers and resolve existing in-flight runs
before starting the matching API and worker revision. Old queued payloads lack
the new manifest pin and fail closed on the new worker. Revalidate/review their
current content before a fresh confirmation. Do not run old and new deployment
workers together: old workers do not enforce the new gates.

These controls do not establish operational readiness of an installation.
Release acceptance still requires actual checks against an authorized sandbox,
the exact deployed artifact, health verification and a demonstrated rollback.

### Current connection and certificate identity

Validation receipts now include the explicit company connection UUID, requested
certificate ID and local RSA public-key SHA-256 alongside the normalized sandbox
account and numeric role. Preview, confirmation and execution require the same
current active or healthy NetSuite connection and encrypted certificate material.
Revocation, replacement, a changed role/certificate/key, another company or a
missing legacy binding blocks execution. No alternate connection is selected.
The connection row is read afresh under a short transaction lock. The runner
releases that lock before credential preparation and external commands so REST
OAuth token refresh can persist rotated tokens without waiting on deployment.
The worker checks identity, permissions, approval, content and receipts again
after credential preparation, and rechecks identity after command return. A
change during command execution prevents success certification and records an
uncertain outcome for reconciliation. It never triggers automatic deployment
replay. Timeout and error receipts distinguish requested intent from verified
pre-command identity.
The signed persistent identity also appears in validation and deployment result
receipts and their audits; private key material is never included.

The future adapter's encrypted connection contract is `account_id` plus a
`suitecloud_ci` object with `role_id`, `certificate_id` and PEM `private_key`.
The local public-key hash is computed from the key's DER SubjectPublicKeyInfo.
These are requested local credential fields; SDK accountInfo verifies only the
account and role. This contract does not provision credentials, enable the SDK
adapter or authorize an external target. Production seeding still always rejects.

Existing account/role-only validation receipts must be replaced by fresh checks.
A fresh preview supersedes an older partially bound preview immediately. Drain
old deployment workers and queued work before activating matching reviewed API
and worker revisions. Trusted SDF construction, isolated credential networking,
a real authorized SDK handshake, target deployment/readback/health/rollback and
all existing release gates remain required.

An OAuth reauthorization currently replaces the encrypted connection credential
blob, dropping any `suitecloud_ci` material. That fails closed and requires a
new explicit certificate binding and fresh checks. No callback preservation or
credential provisioning is enabled here. Before activating the future seeder,
align its connection selection with this explicit UUID/status contract; its
current always-rejecting active-only lookup supplies no execution authority.

Validation deliveries also use the durable exclusive run claim before network
work. Redelivery of a started or completed run does not execute another command;
a fresh validation needs a fresh run. Returned command exit/output are retained
even if the post-command identity check detects drift. Such deployment audits
use `deploy.outcome_uncertain` and require reconciliation before another action.

The trusted file projection has an explicit `suite-studio-files-v1` contract.
Migration 104 stores that version on new confirmations, includes it in the
signed payload and audit, and carries it through durable delivery. Null or
unsupported versions require a fresh preview; they cannot dispatch or reach
credential preparation. Apply the migration and drain old workers/queued work
before activating matching API/worker images. No existing preview is backfilled
with authority.

The full-snapshot projection verifies all original bytes and manifest entries,
including local tests and supported package/Jest inputs. It explicitly maps
`SuiteScripts/` to `FileCabinet/SuiteScripts/`, also accepts canonical script
paths, and generates the fixed file-only SDF project. Only supported script
files enter that project; tests/configuration never enter a credential process.
Deletes, object XML, unknown root paths, destination aliases and all-unchanged
script deployments are unsupported. Project hashes describe the projection and
must not replace the full checked snapshot identity. Serialized manifests must
be canonical; malformed or excessively nested JSON is rejected before any root
is created.

This projection is still an inactive preparation primitive. The runner signs
and checks the version but does not yet execute the generated project with the
fixed Java SDK operations. Preview-time projection readiness, isolated SDK
validate/deploy/readback and their receipts must be integrated before activation.
The production credential seeder remains closed and all target/network/operator
acceptance gates above remain open.

Before any activation, inventory legacy queued confirmations in the exact
company database with this read-only, parameterized query (`:company_id` is
supplied by the authorized operator):

```sql
SELECT r.id, r.status, t.project_format, t.consumed_at
FROM workspace_runs AS r
JOIN workspace_deploy_tokens AS t ON t.consumed_run_id = r.id
  AND t.tenant_id = r.tenant_id AND t.actor_id = r.triggered_by
WHERE r.tenant_id = CAST(:company_id AS uuid)
  AND r.run_type = 'deploy_sandbox' AND r.status = 'queued'
  AND (t.project_format IS NULL OR t.project_format <> 'suite-studio-files-v1');
```

The retry endpoint currently rejects these rows with its generic 404 and a
"preview again" diagnostic; it does not cancel or audit their disposition.
Keep activation blocked until the authorized operator has reconciled and drained
these rows through a reviewed, audited maintenance procedure. Never assign the
new version to an old confirmation or replay a started/uncertain run. This query
is an inventory step, not a cancellation tool or release acceptance proof.

Preview-time projection readiness is still required. Existing sync can produce
`CustomScripts/` paths and space-containing names; v1 does not map those inputs.
Resolve their supported mapping explicitly and reject unsupported snapshots
before issuing a preview when SDK execution is integrated.
