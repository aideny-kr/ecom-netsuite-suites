# Inactive credential executor

This image supports only the fixed broker's offline empty-store diagnostic and
certificate account/role authentication. It cannot validate or deploy a project.
No production caller, image setting or credential provisioning is enabled.

Use a minimal allowlisted build context containing this Dockerfile, the two
listed Python modules and the public Oracle SDK 2026.1.0 JAR at the COPY path.
The image build and SDK session verify the SDK SHA256. Supply a reviewed digest-pinned
Python 3.11 Debian base as BASE_IMAGE, build locally, inspect the final immutable
image ID and review its contents. Do not use the application image or copy a
workspace, .env, SDK store, private key or customer files into the build context.
The fixed image paths are /usr/local/bin/python, /usr/bin/java,
/opt/sdk/cli-2026.1.0.jar and /opt/suitestudio/app/services/workspace/.

The trusted broker owns its Unix Docker socket. Neither tenant commands nor
this executor receives it. Each operation uses a separate PID/user/filesystem
namespace, non-root user, read-only root, ephemeral /tmp, no mounts, dropped
capabilities, no-new-privileges, resource limits and disabled Docker logging.
Only a fixed JSON request enters stdin; only a bounded identity receipt exits.
No project configuration, JS hooks or arbitrary commands are accepted. The
SDK's private home, key and encrypted credential store stay in the namespace.
The SDK is invoked directly; no SuiteCloud Node wrapper runs.

The offline diagnostic has no network. Authentication uses Docker bridge for
outbound SDK traffic, separate from the network-disabled tenant-command
containers. The trusted SDK is the only online executable. Before enabling any
caller, require a dedicated credential network with
inter-container traffic disabled and metadata/host addresses blocked, or
record verified equivalent isolation. Verify Java trust and a real authorized
sandbox handshake under the exact image/resource/tmpfs profile. The default
bridge is not sufficient activation evidence. This prototype supplies no egress
allowlist or target authorization.

PID 1 enforces a 120-second deadline independent of the worker. All descendants
terminate with its namespace. Normal completion, cancellation and failure remove
the container and tmpfs. Versioned leases reuse the command broker's bounded
preflight and operator reaper, so worker crashes and late ambiguous creates are
reclaimed without replay. Provision the periodic reaper and verify clocks,
concurrency, alerts and removal errors before activation. No automatic retry of
an uncertain SDK operation is supported.

Activation additionally requires current permitted connection/account/numeric
role/certificate/public-key identity through validation, preview, confirmation
and worker; a trusted SDF project path; authorized sandbox handshake, checks,
exact-byte deployment/readback/health and rollback acceptance; independent T2
review, CI and the existing release gates. Treat every rebuilt image ID as a
new release artifact requiring inspection
and review: the JRE and transitive pip dependencies are not hash-locked.
The production seeder always rejects.
Global aliases, REST tokens and a passed offline diagnostic grant no authority.
