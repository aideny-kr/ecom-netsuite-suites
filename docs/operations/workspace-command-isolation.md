# Developer workspace command isolation

Project tests execute in a disposable Docker container. The worker never
executes project binaries, npm hooks, Jest configuration or tests on its host.
Without the operator runtime below, a run records `COMMAND_SANDBOX` and an
actionable error. There is no host fallback or network package installation.

This change does not activate SuiteCloud validation or deployment. The
production credential seeder still rejects every attempt. A separate trusted
credential executor, current connection/account/role/certificate identity
binding, and authorized sandbox deployment/readback/health/rollback acceptance
remain required before activation.

## Selected SDK validation routing (inactive runtime)

`POST /api/v1/changesets/{id}/validate` optionally accepts
`{"connection_id":"<company connection UUID>"}`. The server requires the current
builder's permission, an approved changeset, a supported project and the exact
current sandbox connection with certificate configuration. Before queueing, it
stores a bounded selection artifact containing public target identity and run
context, plus an SDK routing marker on the run. Source bytes are captured when
the exclusive execution claim is committed. An omitted body retains the existing
closed seeder path; runtime, keys, image and command options are never accepted.

The worker selects its executor from this durable state. Queue kwargs cannot
replace the connection or supply SDK runtime authority. A missing, duplicate,
corrupt or stale selection blocks the run without falling back to another
executor. The SDK attempt commits a single nonce before broker work and binds
the selection hash to its journal, then rechecks authority, target and source
before dispatch and on return. Signed receipts remain separate from deployment
prerequisites and keep `gate_status=block`. Duplicate or cancelled deliveries do
not replay, and uncertain running attempts require operator reconciliation.

`sdk_validation_dispatch.validation_runtime()` currently always returns `None`.
Selected runs therefore record a blocked failure and explanatory stderr without
calling a broker. No environment setting enables this runtime. Tests inject a
synthetic trusted runtime; their receipts and synthetic Java do not prove a real
NetSuite handshake or authorize deployment. Activation still requires reviewed
image/key custody and rotation, dedicated egress, outcome reconciliation, real
authorized sandbox checks, exact remote-byte verification, health and rollback
acceptance, and the normal release gates. Never reset a claimed run to retry it.

### Inactive receipt-key custody and rotation

`sdk_validation_custody.load_validation_runtime()` prepares a trusted runtime
from an operator-supplied immutable image, local Docker paths and a private
absolute key-directory path. It has no HTTP, queue, environment or project-file
caller. The worker activation seam still returns `None`.

Provision complete generations of key files in a directory owned by the worker
UID or root, mode 0700. Its regular files must be private (0600 or stricter),
owned by that UID or root, with one link. Every parent directory must be owned by
root or that UID and forbid group/other writes, unless sticky directory semantics
prevent other users renaming its children. Symlinks in any path component, special
files, oversized files, duplicate names/keys and unsupported manifest fields are
refused. `manifest` contains only
`{"version":1,"active":"current.key","retained":["previous.key"]}`.
Key files contain 32–4096 raw random bytes from the operator's secret manager;
keys and private paths must never enter source control, tickets, project files,
child environments or image layers. At most seven previous keys are retained.
Python objects hide keys from repr; process memory and privileged debugging
remain within the trusted operator boundary. This loader is not a secret manager.

Prepare rotation in a new private directory, preserving the old generation for
in-flight workers. Atomically change the trusted operator's generation reference
only after provisioning and operational review; do not modify files in place.
Each new durable claim pins a domain-separated HMAC key identifier derived from
its active random key. Never publish a plain SHA-256 key hash: for HMAC-SHA256
keys over 64 bytes that hash is itself the effective signing key.
Readback selects that exact key from the current trusted set and reverifies the
unchanged v1 receipt and current actor, tenant, target, project and image state.
Retained keys are verification-only. Removing a key (including compromise
revocation) blocks its historical receipts without replaying or resigning them.
Removing disk files cannot revoke keys already loaded in process memory. Before
activation, establish worker drain/restart and compromised-key revocation steps;
do not treat disk removal alone as completed revocation.
Record the operator's rotation/revocation decision and affected identifiers in
the private operations record, never the secret bytes.

Legacy claims have no key identifier and retain the original single-active-key
verification behavior. Before retiring their original active key, inventory and
reconcile those attempts with the original pinned runtime. Do not add a guessed
identifier, try all keys, rewrite a receipt or reset a run to obtain a pass.
Signing integrity does not establish deployment authority. Dedicated egress,
operator crash/uncertain-outcome reconciliation and real authorized sandbox,
deployment, exact-byte health and rollback acceptance remain activation gates.

## Operator runtime

- `WORKSPACE_COMMAND_IMAGE`: the full local `sha256:` image ID of a reviewed,
  credential-free image containing `/usr/local/bin/node`, `npx`, and the
  project's locked Jest dependencies. Tags and absent images fail closed;
  the broker never pulls images and rejects image-declared volumes. Image
  hooks, environment and baked files are part of operator review. Do not reuse an application image containing private
  configuration or credentials.
- `WORKSPACE_COMMAND_SOCKET`: an absolute local Docker Unix socket path.
- `WORKSPACE_COMMAND_DOCKER`: the trusted absolute Docker client executable.

The broker needs access to that daemon; tenant code must never receive its
socket or credentials. This is a trusted worker capability, not a reason to
mount the Docker socket into ordinary project containers or expose the daemon
over an unauthenticated network. Production runtime provisioning remains
unconfigured and requires the normal infrastructure release gates.

The only application command currently routed here is the registered Jest
check, with offline resolution and JSON coverage summary. SuiteCloud commands
are rejected by this executor. No arbitrary command/container configuration is
accepted from a tenant.

Jest runs in band so its worker pool cannot exhaust the process/thread budget.
The recorded command is the actual registered argv. A supervisor deadline
retains the existing `TIMEOUT` result category.

## Boundary and lifecycle

The worker reads its private materialized snapshot, rejects links/special
files, and sends bounded UTF-8 content through the Docker client's stdin. The
byte content, including CRLF and lone CR, is preserved. `coverage` is reserved
for generated check output; a project containing that input path is rejected
before execution so source files cannot masquerade as returned coverage.
The container has no host filesystem binds, host PID/IPC namespace, network,
credential environment, or writable image filesystem. Project and temporary
files live on size-limited tmpfs mounts. PID, memory, CPU and output limits are
fixed by trusted code.

A trusted Node supervisor is PID 1, running as root with only SETUID/SETGID
capabilities. It starts project code as uid/gid 65532 with no capabilities and
no privilege escalation. The separate uid prevents project children from
stopping the supervisor's deadline or forging its output envelope. The
supervisor executes no project code itself. Its deadline survives worker
termination. Exiting the PID namespace kills detached descendants too.

The worker independently bounds attached output and verifies the container's
exit state and supervisor receipt. Only bounded stdout/stderr and a validated
JSON coverage summary return; the latter is copied to the disposable snapshot
for existing artifact storage. Source files are never copied back. Results
remain project-controlled check evidence, not independent authorization or
proof of external deployment.

Every exit, timeout, cancellation and uncertain create response attempts
forced deletion by a fresh name chosen before creation, including anonymous
volumes. Creation/execution is not retried. Cleanup failure is an error, never
a successful check. A killed worker can leave stopped metadata after the
supervisor deadline; an ambiguous create can finish after deletion returned
404 and leave unstarted metadata. Neither case retries execution.

## Expired container reconciliation

Before creation, the broker assigns immutable versioned lease labels: a fresh
lease UUID, expiry covering the full command budget plus 120 seconds, and the
existing workspace run, tenant and workspace UUIDs. These labels survive a
worker crash without storing source or credentials. Direct diagnostic calls
are explicitly labeled diagnostic rather than claiming database run identity.

Each command sweeps expired leases before creating more work. Operators can
also run the same bounded sweep from the trusted broker environment:

```sh
python -m app.services.workspace.command_sandbox --socket /var/run/docker.sock
```

The sweep lists only versioned broker leases, inspects each immutable container
ID, checks the name/lease/identity/image/expiry, then force-removes only expired
containers by ID, including volumes. Unexpired, legacy and unrecognized
resources are retained. It never starts a container, recreates a run or retries
a target action. Inventory over 100 containers, daemon failures and cleanup
failures return an error; new commands do not launch after a failed preflight
sweep. Concurrent removal conflicts receive one read-back: absence or verified
removal in progress is safe; other conflicts fail closed without another
delete. The sweep has a 30-second total bound and returns counts only. A later
sweep reclaims a create that finished after an earlier delete returned 404.

Before activation, provision and verify a periodic sweep in the trusted broker
environment so residue is reclaimed even when no new commands arrive. No
production timer or broker runtime is provisioned by this code change. Verify
the actual socket/image, crash and late-create recovery, retained active and
unrelated containers, zero residue, and operator error reporting under the
normal infrastructure release gates. The separate credential executor and
authorized target acceptance remain required.
