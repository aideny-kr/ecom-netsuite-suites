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

## Operator runtime

- `WORKSPACE_COMMAND_IMAGE`: the full local `sha256:` image ID of a reviewed,
  credential-free image containing `/usr/local/bin/node`, `npx`, and the
  project's locked Jest dependencies. Tags and absent images fail closed;
  the broker never pulls images. Image hooks, environment and baked files are
  part of operator review. Do not reuse an application image containing private
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

## Boundary and lifecycle

The worker reads its private materialized snapshot, rejects links/special
files, and sends bounded UTF-8 content through the Docker client's stdin. The
container has no host filesystem binds, host PID/IPC namespace, network,
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
a successful check. A killed worker can leave a stopped container after the
supervisor deadline; operators must inspect the
`suitestudio.workspace-command=1` label and remove confirmed stale resources.
Do not delete another active run. Automatic crash-residue reconciliation and
production broker provisioning are not certified by this change.
