"""Inactive fixed-operation SDK broker; never accepts tenant code or project files.

Trusted operator configuration supplies a reviewed immutable image containing
only Python, Java, cryptography and the pinned SDK module/artifact. Credentials
travel over stdin into a private disposable namespace, never Docker metadata,
workspace files, returned artifacts or the worker's SDK store. No production
caller exists; connection authority and receipt binding remain separate gates.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.workspace import command_sandbox as broker
from app.services.workspace.credential_contract import AuthSeederError
from app.services.workspace.suitecloud_sdk import VerifiedIdentity

MAX_INPUT = 32 * 1024
MAX_RECEIPT = 4096
TIMEOUT = 120


class CredentialExecutorError(AuthSeederError):
    """Sanitized failure; unknown outcomes never trigger automatic replay."""


# PID 1 owns an independent deadline. No project, arbitrary argv, hook, import
# path or environment is accepted in the request. Exiting PID 1 tears down all
# descendants, including detached SDK children, after a killed worker.
_BOOTSTRAP = r"""
import asyncio, dataclasses, json, os, signal, sys
from pathlib import Path
signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
signal.alarm(120)
sys.path.insert(0, "/opt/suitestudio")
from app.services.workspace.suitecloud_sdk import IsolatedSdkSession
async def execute():
    raw = sys.stdin.buffer.read(32769)
    if len(raw) > 32768: raise ValueError()
    request = json.loads(raw)
    if not isinstance(request, dict): raise ValueError()
    op = request.get("operation")
    if op == "verify_empty_store":
        if set(request) != {"operation"}: raise ValueError()
    elif op == "authenticate":
        if set(request) != {"operation", "account_id", "role_id", "certificate_id", "private_key"}: raise ValueError()
        if not all(isinstance(v, str) for v in request.values()): raise ValueError()
    else: raise ValueError()
    async with IsolatedSdkSession(sdk_jar=Path("/opt/sdk/cli-2026.1.0.jar"), java=Path("/usr/bin/java")) as session:
        if op == "verify_empty_store":
            await session.verify_empty_store()
            return {"empty_store": True}
        result = await session.authenticate(account_id=request["account_id"], role_id=request["role_id"],
            certificate_id=request["certificate_id"], private_key=request["private_key"].encode("ascii"))
        return dataclasses.asdict(result)
try:
    result = asyncio.run(execute())
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
except BaseException:
    os._exit(125)
"""


def _spec(image: str, *, online: bool) -> dict:
    spec = broker._container_spec(image, [], TIMEOUT)
    spec.update(
        User="65532:65532",
        Entrypoint=["/usr/local/bin/python", "-I", "-c", _BOOTSTRAP],
        Cmd=[],
        Env=["HOME=/tmp", "TMPDIR=/tmp", "LANG=C.UTF-8", "LD_PRELOAD=", "LD_LIBRARY_PATH="],
        NetworkDisabled=not online,
    )
    host = spec["HostConfig"]
    host.pop("CapAdd")
    host["NetworkMode"] = "bridge" if online else "none"
    host["PidsLimit"] = 128
    host["Memory"] = host["MemorySwap"] = 768 * 1024 * 1024
    host["Tmpfs"] = {"/tmp": "rw,nosuid,nodev,noexec,size=128m,mode=1777"}
    return spec


def _identity(account_id: str, role_id: str, certificate_id: str, private_key: bytes) -> VerifiedIdentity:
    account = account_id.strip().lower().replace("_", "-")
    if (
        not re.fullmatch(r"[a-z0-9]+-sb[0-9]+", account, re.ASCII)
        or not re.fullmatch(r"[1-9][0-9]*", role_id, re.ASCII)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", certificate_id, re.ASCII)
    ):
        raise CredentialExecutorError("Credential executor requires explicit sandbox, numeric role and certificate")
    try:
        if not isinstance(private_key, bytes) or len(private_key) > 16 * 1024:
            raise ValueError()
        private_key.decode("ascii")
        key = serialization.load_pem_private_key(private_key, password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError()
    except (TypeError, ValueError):
        raise CredentialExecutorError("Credential executor requires a valid RSA private key") from None
    fingerprint = hashlib.sha256(
        key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    ).hexdigest()
    return VerifiedIdentity(account, role_id, certificate_id, fingerprint)


async def _execute(request: dict, *, image: str, socket: str, docker: str) -> dict:
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise CredentialExecutorError("Credential executor requires a reviewed immutable image")
    if not Path(socket).is_absolute() or not Path(docker).is_absolute():
        raise CredentialExecutorError("Credential executor runtime configuration is invalid")
    payload = json.dumps(request).encode("utf-8")
    if len(payload) > MAX_INPUT:
        raise CredentialExecutorError("Credential executor input exceeds limit")
    lease = uuid.uuid4().hex
    name = "ss-command-" + lease  # Shares the verified versioned broker reaper.
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=socket),
        base_url="http://docker/v1.45",
        timeout=10,
        trust_env=False,
    ) as client:
        try:
            response = await client.get(f"/images/{image}/json")
            response.raise_for_status()
            metadata = response.json()
            if (
                not isinstance(metadata, dict)
                or metadata.get("Id") != image
                or not isinstance(metadata.get("Config"), dict)
            ):
                raise CredentialExecutorError("Credential executor image identity could not be verified")
            if metadata["Config"].get("Volumes"):
                raise CredentialExecutorError("Credential executor image cannot declare persistent volumes")
            async with asyncio.timeout(30):
                await broker._reconcile_expired(client, now=int(time.time()), limit=100)
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, broker.CommandSandboxError):
            raise CredentialExecutorError(
                "Credential executor preflight unavailable; operator inspection required"
            ) from None
        try:
            async with asyncio.timeout(TIMEOUT + 15):
                spec = _spec(image, online=request["operation"] == "authenticate")
                spec["Labels"] = broker._lease_labels(lease, TIMEOUT)
                spec["Labels"][broker.LEASE_LABEL + ".purpose"] = "credentials"
                response = await client.post("/containers/create", params={"name": name}, json=spec)
                response.raise_for_status()
                with tempfile.TemporaryDirectory(prefix="ss-credential-client-") as config_dir:
                    code, stdout, stderr = await broker._attach(
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
                        TIMEOUT,
                    )
                response = await client.get(f"/containers/{name}/json")
                response.raise_for_status()
                state = response.json()["State"]
                if not isinstance(state, dict):
                    raise ValueError()
                if state.get("ExitCode") == 124:
                    raise TimeoutError
                if state.get("Running") or state.get("OOMKilled") or state.get("ExitCode") != 0 or code != 0:
                    raise CredentialExecutorError("Credential executor failed; reconcile before retry")
                if stderr or len(stdout.encode("utf-8")) > MAX_RECEIPT:
                    raise CredentialExecutorError("Credential executor returned an invalid receipt")
                result = json.loads(stdout)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except TimeoutError:
            raise
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, broker.CommandSandboxError):
            raise CredentialExecutorError("Credential executor failed; reconcile before retry") from None
        finally:

            async def remove():
                try:
                    async with asyncio.timeout(12):
                        response = await client.delete(f"/containers/{name}", params={"force": 1, "v": 1})
                    if response.status_code not in (204, 404):
                        raise CredentialExecutorError(
                            "Credential executor cleanup failed; operator inspection required"
                        )
                except (httpx.HTTPError, TimeoutError):
                    raise CredentialExecutorError(
                        "Credential executor cleanup unavailable; operator inspection required"
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


async def verify_empty_store(*, image: str, socket: str, docker: str) -> None:
    """Offline diagnostic; no credential/target/account is supplied."""
    result = await _execute({"operation": "verify_empty_store"}, image=image, socket=socket, docker=docker)
    if set(result) != {"empty_store"} or result["empty_store"] is not True:
        raise CredentialExecutorError("Credential executor isolated store is not empty")


async def authenticate(
    *,
    account_id: str,
    role_id: str,
    certificate_id: str,
    private_key: bytes,
    image: str,
    socket: str,
    docker: str,
) -> VerifiedIdentity:
    """Internal primitive, not target authorization or a deployment operation.

    Only the fixed pinned Java SDK runs online; tenant code cannot join this
    namespace or network. No credential store or arbitrary output is returned.
    Current connection authorization must be checked before any future caller.
    """
    expected = _identity(account_id, role_id, certificate_id, private_key)
    request = {
        "operation": "authenticate",
        "account_id": expected.account_id,
        "role_id": role_id,
        "certificate_id": certificate_id,
        "private_key": private_key.decode("ascii"),
    }
    result = await _execute(request, image=image, socket=socket, docker=docker)
    if result != asdict(expected):
        raise CredentialExecutorError("Credential executor identity does not match the requested authorization")
    return expected
