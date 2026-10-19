"""Broker tenant-authored tests into a disposable, credential-free container.

Only the trusted worker talks to a local Docker socket. No tenant code runs
on that host. A reviewed immutable test image is operator configuration; an
absent runtime fails closed. This does not enable SuiteCloud authorization.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import signal
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx

MAX_OUTPUT = 256 * 1024
MAX_WIRE_OUTPUT = MAX_OUTPUT * 18 + 8192
MAX_INPUT = 50 * 1024 * 1024
LEASE_LABEL = "suitestudio.workspace-command"
current_command_scope = contextvars.ContextVar("workspace_command_scope", default=None)


class CommandSandboxError(Exception):
    """Safe runtime diagnostic; never expose Docker responses or host paths."""


@contextmanager
def command_run_scope(run_id: uuid.UUID, tenant_id: uuid.UUID, workspace_id: uuid.UUID):
    """Correlate Docker metadata with an existing run without copying content."""
    scope = (run_id, tenant_id, workspace_id)
    if not all(isinstance(value, uuid.UUID) for value in scope):
        raise CommandSandboxError("Command sandbox run identity is invalid")
    token = current_command_scope.set(scope)
    try:
        yield
    finally:
        current_command_scope.reset(token)


def _lease_labels(lease: str, timeout: int) -> dict[str, str]:
    labels = {
        LEASE_LABEL: "1",
        LEASE_LABEL + ".protocol": "1",
        LEASE_LABEL + ".lease": lease,
        # Include the whole create/attach budget plus cleanup grace. An
        # ambiguous create finishing later is still discoverable by expiry.
        LEASE_LABEL + ".expires": str(int(time.time()) + timeout + 30),
        LEASE_LABEL + ".scope": "diagnostic",
    }
    scope = current_command_scope.get()
    if scope is not None:
        labels[LEASE_LABEL + ".scope"] = "workspace"
        for name, value in zip(("run", "tenant", "workspace"), scope):
            labels[LEASE_LABEL + "." + name] = str(value)
    return labels


def _expired_lease(metadata: dict, container_id: str, now: int) -> bool:
    """Inspect ownership again; never delete by a reusable container name."""
    try:
        config = metadata["Config"]
        labels = config["Labels"]
        lease, expires = labels[LEASE_LABEL + ".lease"], labels[LEASE_LABEL + ".expires"]
        scope = labels[LEASE_LABEL + ".scope"]
        if (
            metadata["Id"] != container_id
            or labels.get(LEASE_LABEL) != "1"
            or labels.get(LEASE_LABEL + ".protocol") != "1"
            or not re.fullmatch(r"[a-f0-9]{32}", lease)
            or metadata["Name"] != "/ss-command-" + lease
            or not re.fullmatch(r"[0-9]{1,11}", expires)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", config["Image"])
            or scope not in ("workspace", "diagnostic")
        ):
            return False
        if scope == "workspace":
            for name in ("run", "tenant", "workspace"):
                value = labels[LEASE_LABEL + "." + name]
                if str(uuid.UUID(value)) != value:
                    return False
        return int(expires) <= now
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


async def _reconcile_expired(client: httpx.AsyncClient, *, now: int, limit: int) -> dict[str, int]:
    response = await client.get(
        "/containers/json",
        params={
            "all": 1,
            "limit": limit + 1,
            "filters": json.dumps({"label": [LEASE_LABEL + "=1", LEASE_LABEL + ".protocol=1"]}),
        },
    )
    response.raise_for_status()
    entries = response.json()
    if not isinstance(entries, list):
        raise CommandSandboxError("Command sandbox reconciliation returned an invalid inventory")
    if len(entries) > limit:
        raise CommandSandboxError(
            "Command sandbox reconciliation inventory exceeds limit; operator inspection required"
        )
    result = {"scanned": len(entries), "removed": 0, "retained": 0}
    for entry in entries:
        container_id = entry.get("Id") if isinstance(entry, dict) else None
        if not isinstance(container_id, str) or not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise CommandSandboxError("Command sandbox reconciliation returned an invalid container identity")
        response = await client.get(f"/containers/{container_id}/json")
        if response.status_code == 404:
            continue
        response.raise_for_status()
        if not _expired_lease(response.json(), container_id, now):
            result["retained"] += 1
            continue
        response = await client.delete(f"/containers/{container_id}", params={"force": 1, "v": 1})
        if response.status_code not in (204, 404):
            raise CommandSandboxError("Command sandbox reconciliation cleanup failed; operator inspection required")
        result["removed"] += 1
    return result


async def reconcile_expired_commands(*, socket: str, now: int | None = None, limit: int = 100) -> dict[str, int]:
    """Bounded operator/broker sweep. Never creates, starts or retries a run.

    Uses Docker's immutable IDs and versioned leases only. Legacy/unrecognized
    containers are retained for inspection. Periodic operator provisioning is
    still required to reclaim metadata when no new commands arrive.
    """
    if not Path(socket).is_absolute() or not 1 <= limit <= 100:
        raise CommandSandboxError("Command sandbox reconciliation configuration is invalid")
    try:
        async with asyncio.timeout(30):
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=socket),
                base_url="http://docker/v1.45",
                timeout=10,
                trust_env=False,
            ) as client:
                return await _reconcile_expired(client, now=int(time.time()) if now is None else now, limit=limit)
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
        raise CommandSandboxError("Command sandbox reconciliation unavailable; operator inspection required") from None


def _snapshot(cwd: str) -> bytes:
    root = Path(cwd)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise CommandSandboxError("Command sandbox requires a materialized project")
    if (root / "coverage").exists():
        raise CommandSandboxError("The coverage path is reserved for check output; remove it from project inputs")
    size = count = 0
    files = {}
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise CommandSandboxError("Command sandbox rejects links and special files")
        count += 1
        size += path.stat().st_size if stat.S_ISREG(mode) else 0
        if count > 10000 or size > MAX_INPUT:
            raise CommandSandboxError("Command sandbox project exceeds input limit")
        if stat.S_ISREG(mode):
            try:
                files[path.relative_to(root).as_posix()] = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeError):
                raise CommandSandboxError("Command sandbox requires readable UTF-8 project files") from None
    payload = json.dumps(files, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_INPUT:
        raise CommandSandboxError("Command sandbox project exceeds input limit")
    return payload


# This trusted Node parent is PID 1. Tenant code is always a child. Its timer
# and output limits survive a worker crash; namespace teardown kills setsid
# descendants too. Project files arrive on stdin, with NO host filesystem bind.
_BOOTSTRAP = r"""
const fs = require("fs"), path = require("path"), cp = require("child_process");
const limit = 262144;
process.umask(0);
setTimeout(() => process.exit(124), Number(process.argv[1]) * 1000);
let chunks = [], size = 0;
process.stdin.on("data", chunk => {
  size += chunk.length;
  if (size > 52428800) process.exit(125);
  chunks.push(chunk);
});
process.stdin.on("end", () => {
  try {
    const root = "/work/project";
    fs.mkdirSync(root, {mode:0o777});
    const files = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    chunks = [];
    for (const [name, content] of Object.entries(files)) {
      const target = path.resolve(root, name);
      if (!target.startsWith(root + "/") || typeof content !== "string") process.exit(125);
      fs.mkdirSync(path.dirname(target), {recursive:true, mode:0o777});
      fs.writeFileSync(target, content, {mode:0o777, flag:"wx"});
    }
    const cmd = JSON.parse(process.argv[2]);
    const child = cp.spawn(cmd[0], cmd.slice(1), {
      cwd:root, uid:65532, gid:65532, stdio:["ignore", "pipe", "pipe"],
      env:{PATH:"/usr/local/bin:/usr/bin:/bin", HOME:"/tmp", TMPDIR:"/tmp", CI:"1", npm_config_offline:"true"}
    });
    const output = {stdout:[], stderr:[]};
    for (const [source, key] of [[child.stdout, "stdout"], [child.stderr, "stderr"]]) {
      let bytes = 0;
      source.on("data", chunk => {
        bytes += chunk.length;
        if (bytes > limit) process.exit(125);
        output[key].push(chunk);
      });
    }
    child.on("error", () => process.exit(126));
    child.on("close", code => {
      const exit = Number.isInteger(code) ? code : 127;
      let coverage = null, fd;
      const target = root + "/coverage/coverage-summary.json";
      try {
        fd = fs.openSync(target, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
        if (fs.fstatSync(fd).isFile()) {
          const data = Buffer.alloc(limit + 1), size = fs.readSync(fd, data, 0, limit + 1, null);
          if (size <= limit) coverage = data.subarray(0, size).toString("utf8");
        }
      } catch (_) {} finally {if (fd !== undefined) fs.closeSync(fd);}
      const result = {exit_code:exit, stdout:Buffer.concat(output.stdout).toString("utf8"),
        stderr:Buffer.concat(output.stderr).toString("utf8"), coverage_summary:coverage};
      process.stdout.write(JSON.stringify(result), () => process.exit(exit));
    });
  } catch (_) { process.exit(125); }
});
"""


def _container_spec(image: str, cmd: list[str], timeout: int) -> dict:
    return {
        "Image": image,
        # Only the trusted supervisor is root, with the two identity-change
        # capabilities. It launches project code as 65532 with no capabilities;
        # project children cannot signal/stop the supervisor or its deadline.
        "User": "0:0",
        "WorkingDir": "/tmp",
        "Entrypoint": ["/usr/local/bin/node", "-e", _BOOTSTRAP],
        "Cmd": [str(timeout), json.dumps(cmd)],
        "OpenStdin": True,
        "StdinOnce": True,
        "Env": [
            "HOME=/tmp",
            "TMPDIR=/tmp",
            "CI=1",
            "npm_config_offline=true",
            "NODE_OPTIONS=",
            "NODE_PATH=",
            "LD_PRELOAD=",
            "LD_LIBRARY_PATH=",
        ],
        "Healthcheck": {"Test": ["NONE"]},
        "Labels": {"suitestudio.workspace-command": "1"},
        "NetworkDisabled": True,
        "HostConfig": {
            "NetworkMode": "none",
            "IpcMode": "private",
            "CgroupnsMode": "private",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "CapAdd": ["SETUID", "SETGID"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PidsLimit": 64,
            "Memory": 512 * 1024 * 1024,
            "MemorySwap": 512 * 1024 * 1024,
            "NanoCpus": 1000000000,
            "Ulimits": [{"Name": "nofile", "Soft": 256, "Hard": 256}],
            "Tmpfs": {
                "/tmp": "rw,nosuid,nodev,noexec,size=32m,mode=1777",
                "/work": "rw,nosuid,nodev,size=128m,mode=1777",
            },
            "Mounts": [],
            "LogConfig": {"Type": "none"},
        },
    }


class _AttachedOutput(asyncio.SubprocessProtocol):
    def __init__(self):
        self.result = asyncio.get_running_loop().create_future()
        self.exited = asyncio.get_running_loop().create_future()
        self.output = {1: bytearray(), 2: bytearray()}
        self.open_pipes = {1, 2}

    def pipe_data_received(self, fd, data):
        if self.result.done():
            return
        if len(self.output[fd]) + len(data) > MAX_WIRE_OUTPUT:
            self.result.set_exception(CommandSandboxError("Command sandbox output limit exceeded"))
        else:
            self.output[fd].extend(data)

    def pipe_connection_lost(self, fd, exc):
        self.open_pipes.discard(fd)
        if exc is not None and fd in (1, 2) and not self.result.done():
            self.result.set_exception(CommandSandboxError("Command sandbox output stream failed"))
        self._complete()

    def process_exited(self):
        self.exited.set_result(None)
        self._complete()

    def _complete(self):
        if self.exited.done() and not self.open_pipes and not self.result.done():
            self.result.set_result(tuple(self.output[s].decode("utf-8", errors="replace") for s in (1, 2)))


async def _attach(cmd: list[str], payload: bytes, timeout: int) -> tuple[int, str, str]:
    # The host executes only the trusted Docker client, never project binaries.
    try:
        transport, protocol = await asyncio.get_running_loop().subprocess_exec(
            _AttachedOutput,
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin"},
            start_new_session=True,
        )
    except OSError:
        raise CommandSandboxError("Command sandbox Docker client is unavailable") from None
    try:
        stdin = transport.get_pipe_transport(0)
        stdin.write(payload)
        stdin.write_eof()
        async with asyncio.timeout(timeout + 5):
            stdout, stderr = await protocol.result
        return transport.get_returncode(), stdout, stderr
    finally:
        try:
            os.killpg(transport.get_pid(), signal.SIGKILL)
        except ProcessLookupError:
            pass
        transport.close()
        try:
            async with asyncio.timeout(0.5):
                await asyncio.shield(protocol.exited)
        except TimeoutError:
            pass


async def run_isolated_command(
    cmd: list[str], cwd: str, timeout: int, *, image: str, socket: str, docker: str
) -> tuple[int, str, str]:
    """Execute without network, host credentials, writable binds or host PID access.

    The caller is trusted application code, never a tenant-supplied container
    specification. The existing runner registry owns command selection.
    """
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise CommandSandboxError("Workspace tests require a reviewed immutable command image")
    if not Path(socket).is_absolute() or not Path(docker).is_absolute() or not 1 <= timeout <= 600:
        raise CommandSandboxError("Command sandbox runtime configuration is invalid")
    try:
        payload = _snapshot(cwd)
    except OSError:
        raise CommandSandboxError("Command sandbox project cannot be read") from None
    lease = uuid.uuid4().hex
    name = "ss-command-" + lease
    transport = httpx.AsyncHTTPTransport(uds=socket)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://docker/v1.45", timeout=10, trust_env=False
    ) as client:
        try:
            response = await client.get(f"/images/{image}/json")
            response.raise_for_status()
            metadata = response.json()
            if metadata.get("Id") != image or not isinstance(metadata.get("Config"), dict):
                raise CommandSandboxError("Command sandbox image identity could not be verified")
            if metadata["Config"].get("Volumes"):
                raise CommandSandboxError("Command sandbox image cannot declare persistent volumes")
        except httpx.HTTPError:
            raise CommandSandboxError("Command sandbox immutable image is unavailable") from None
        # Sweep before issuing any new create. A failed sweep must not launch
        # more work while cleanup is unavailable or the inventory is over budget.
        try:
            async with asyncio.timeout(30):
                await _reconcile_expired(client, now=int(time.time()), limit=100)
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
            raise CommandSandboxError(
                "Command sandbox reconciliation unavailable; operator inspection required"
            ) from None
        # A generated name is known before create: even an ambiguous creation
        # response can be removed without repeating creation or execution.
        try:
            async with asyncio.timeout(timeout + 15):
                spec = _container_spec(image, cmd, timeout)
                spec["Labels"] = _lease_labels(lease, timeout)
                response = await client.post(
                    "/containers/create",
                    params={"name": name},
                    json=spec,
                )
                response.raise_for_status()
                with tempfile.TemporaryDirectory(prefix="ss-docker-client-") as config_dir:
                    cli_exit, stdout, stderr = await _attach(
                        [
                            docker,
                            "--host",
                            "unix://" + socket,
                            "--config",
                            config_dir,
                            "start",
                            "--attach",
                            "--interactive",
                            name,
                        ],
                        payload,
                        timeout,
                    )
                response = await client.get(f"/containers/{name}/json")
                response.raise_for_status()
                state = response.json()["State"]
                if state.get("Running") or not isinstance(state.get("ExitCode"), int):
                    raise CommandSandboxError("Command sandbox execution failed")
                if state["ExitCode"] == 124:
                    raise TimeoutError
                if state.get("OOMKilled") or state["ExitCode"] in (125, 126, 127) or cli_exit != state["ExitCode"]:
                    raise CommandSandboxError("Command sandbox exceeded limits or could not execute tests")
                try:
                    result = json.loads(stdout)
                    if stderr or result["exit_code"] != state["ExitCode"]:
                        raise ValueError
                    for key in ("stdout", "stderr"):
                        if not isinstance(result[key], str) or len(result[key].encode("utf-8")) > MAX_OUTPUT:
                            raise ValueError
                    coverage = result.get("coverage_summary")
                    if coverage is not None:
                        if not isinstance(coverage, str) or len(coverage.encode("utf-8")) > MAX_OUTPUT:
                            raise ValueError
                        if not isinstance(json.loads(coverage), dict):
                            raise ValueError
                except (ValueError, KeyError, TypeError):
                    raise CommandSandboxError("Command sandbox returned an invalid result receipt") from None
                if coverage is not None:
                    # Return only this bounded artifact to the disposable
                    # materialized snapshot; never copy a container filesystem.
                    try:
                        coverage_dir = Path(cwd) / "coverage"
                        coverage_dir.mkdir(exist_ok=True)
                        (coverage_dir / "coverage-summary.json").write_text(coverage, encoding="utf-8")
                    except OSError:
                        raise CommandSandboxError("Command sandbox coverage artifact could not be stored") from None
                return state["ExitCode"], result["stdout"], result["stderr"]
        except TimeoutError:
            raise
        except (httpx.HTTPError, OSError):
            raise CommandSandboxError("Command sandbox runtime is unavailable") from None
        finally:

            async def remove():
                try:
                    async with asyncio.timeout(12):
                        response = await client.delete(f"/containers/{name}", params={"force": 1, "v": 1})
                    if response.status_code not in (204, 404):
                        raise CommandSandboxError("Command sandbox cleanup failed; operator inspection required")
                except (httpx.HTTPError, TimeoutError):
                    raise CommandSandboxError(
                        "Command sandbox cleanup unavailable; operator inspection required"
                    ) from None

            cleanup = asyncio.create_task(remove())
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Reconcile expired workspace command containers; never restart work")
    parser.add_argument("--socket", required=True, help="Trusted broker Docker Unix socket")
    parser.add_argument("--limit", type=int, default=100, help="Maximum inventory size (1-100); excess fails closed")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(reconcile_expired_commands(socket=args.socket, limit=args.limit))))
    except CommandSandboxError as exc:
        parser.exit(1, str(exc) + "\n")
