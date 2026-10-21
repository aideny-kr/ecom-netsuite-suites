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
