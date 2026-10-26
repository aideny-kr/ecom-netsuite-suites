"""Isolated certificate authentication primitive for Oracle SDK 2026.1.

Not connected to the workspace runner. The production seeder stays closed
until a trusted SDF project, credential network and real SDK handshake are
verified with authorized target acceptance. Current connection/certificate/key
binding is enforced separately; it does not activate this primitive.

Use the SDK's integration protocol directly: workspace JavaScript hooks and
project configuration must never execute in this credential-bearing process.
An inactive, single-use server-validation primitive accepts only a rebuilt
reviewed projection. Deployment and remote readback remain unimplemented.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.workspace.credential_contract import AuthSeederError
from app.services.workspace.trusted_sdf_project import (
    TrustedProjectError,
    build_ready_reviewed_project,
    materialize_project,
)

SDK_VERSION = "2026.1.0"
SDK_SHA256 = "158038cfdc552e1af4b29aea86ff59ba8c0658ec950129339c1fbace4b1ad78b"
MAX_OUTPUT_BYTES = 64 * 1024
_SANDBOX_ACCOUNT = re.compile(r"[a-z0-9]+-sb[0-9]+", re.ASCII)
_ROLE = re.compile(r"[1-9][0-9]*", re.ASCII)


class SdkAuthError(AuthSeederError):
    """Safe, deliberately non-raw diagnostic for the credential boundary."""


@dataclass(frozen=True)
class VerifiedIdentity:
    account_id: str
    role_id: str
    # SDK accountInfo verifies account/role only. These two fields describe
    # the requested credential, not independently introspected certificate data.
    requested_certificate_id: str
    local_public_key_sha256: str


@dataclass(frozen=True)
class SdkValidationReceipt:
    identity: VerifiedIdentity
    source_snapshot_sha: str
    source_manifest_sha: str
    project_format: str
    projected_snapshot_sha: str
    projected_manifest_sha: str
    project_sha: str
    sdk_output_sha256: str


async def _settle_owned_task(task):
    """Finish owned filesystem work despite repeated caller cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
    return task.result()


@asynccontextmanager
async def _project_directory(project):
    """Offload filesystem work, settle cleanup, then propagate cancellation."""
    owner = asyncio.current_task()
    cancellations_before = owner.cancelling()
    context = materialize_project(project)
    allocation = asyncio.create_task(asyncio.to_thread(context.__enter__))
    try:
        yield await asyncio.shield(allocation)
    finally:
        try:
            await _settle_owned_task(allocation)
            cleanup = asyncio.create_task(asyncio.to_thread(context.__exit__, None, None, None))
            await _settle_owned_task(cleanup)
        finally:
            if owner.cancelling() > cancellations_before:
                raise asyncio.CancelledError()


def _verify_project_bytes(root, project):
    paths = list(root.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise SdkAuthError("SuiteCloud SDK validation changed the generated project")
    actual = {path.relative_to(root).as_posix(): path for path in paths if path.is_file()}
    if set(actual) != set(project.files) or any(
        actual[name].stat().st_size != len(content) or actual[name].read_bytes() != content
        for name, content in project.files.items()
    ):
        raise SdkAuthError("SuiteCloud SDK validation changed the generated project")


class _BoundedOutput(asyncio.SubprocessProtocol):
    """Own public pipe transports so detached children cannot stall cleanup."""

    def __init__(self):
        self.result = asyncio.get_running_loop().create_future()
        self.exited = asyncio.get_running_loop().create_future()
        self.stdout = bytearray()
        self.sizes = {1: 0, 2: 0}
        self.open_pipes = {1, 2}

    def pipe_data_received(self, fd, data):
        if self.result.done():
            return
        self.sizes[fd] += len(data)
        if self.sizes[fd] > MAX_OUTPUT_BYTES:
            self.result.set_exception(SdkAuthError("SuiteCloud SDK output limit exceeded"))
        elif fd == 1:
            self.stdout.extend(data)

    def pipe_connection_lost(self, fd, exc):
        self.open_pipes.discard(fd)
        if exc is not None and not self.result.done():
            self.result.set_exception(SdkAuthError("SuiteCloud SDK output stream failed"))
        self._complete()

    def process_exited(self):
        if not self.exited.done():
            self.exited.set_result(None)
        self._complete()

    def _complete(self):
        if self.exited.done() and not self.open_pipes and not self.result.done():
            self.result.set_result(bytes(self.stdout))


async def _run_process(cmd: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> bytes:
    """Bound output, runtime and cleanup; kill the owned process group.

    Detached children require the dedicated credential container/uid boundary;
    this primitive is not a sandbox for arbitrary executable code.
    """
    transport, protocol = await asyncio.get_running_loop().subprocess_exec(
        _BoundedOutput,
        *cmd,
        cwd=cwd,
        env=env,
        start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout):
            stdout = await protocol.result
        if transport.get_returncode() != 0:
            raise SdkAuthError("SuiteCloud SDK authentication process failed")
        return stdout
    finally:
        try:
            os.killpg(transport.get_pid(), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        # Public transport close discards pipe data instead of an unbounded
        # communicate() drain. Escaped children cannot hold our pipes open.
        transport.close()
        try:
            async with asyncio.timeout(0.5):
                await asyncio.shield(protocol.exited)
        except TimeoutError:
            pass


class IsolatedSdkSession:
    """Single-use credential context; caller must use ``async with``.

    Runtime paths are trusted operator inputs, never workspace files. No
    inherited JVM/Node options, SDK aliases, passkeys or credentials are read.
    Private directories do not isolate same-uid tenant code. Before connecting
    a production caller, require a dedicated credential host/container that
    cannot co-host or be inspected by workspace commands. Certificate setup
    remains a separate authorized installation action.
    """

    def __init__(self, *, sdk_jar: Path, java: Path):
        self._source = sdk_jar
        self._java = java
        self._entered = False
        self._open = False
        self._attempted = False
        self._verified_identity: VerifiedIdentity | None = None
        self._validation_attempted = False
        self._env: dict[str, str] = {}
        self.auth_id = "fw_" + secrets.token_hex(16)

    async def __aenter__(self):
        if self._entered:
            raise SdkAuthError("SuiteCloud SDK session cannot be reused")
        self._entered = True
        try:
            if not self._java.is_absolute() or not self._java.is_file() or not os.access(self._java, os.X_OK):
                raise SdkAuthError("SuiteCloud SDK requires a trusted absolute Java executable")
            if self._source.stat().st_size > 100 * 1024 * 1024:
                raise SdkAuthError("SuiteCloud SDK artifact is too large")
            artifact = self._source.read_bytes()
        except OSError:
            raise SdkAuthError("SuiteCloud SDK artifact is unavailable") from None
        if hashlib.sha256(artifact).hexdigest() != SDK_SHA256:
            raise SdkAuthError("SuiteCloud SDK artifact does not match the pinned digest")

        self.root = Path(tempfile.mkdtemp(prefix="suitecloud_sdk_"))
        try:
            self._jar = self.root / f"cli-{SDK_VERSION}.jar"
            self._jar.write_bytes(artifact)
            self._jar.chmod(0o600)
            self._env = {
                "HOME": str(self.root),
                "TMPDIR": str(self.root),
                "SUITECLOUD_CI": "1",
                "SUITECLOUD_CI_PASSKEY": secrets.token_hex(32),
            }
            self._open = True
            return self
        except BaseException:
            shutil.rmtree(self.root)
            raise

    async def __aexit__(self, *_):
        self._open = False
        self._verified_identity = None
        self._env.clear()
        shutil.rmtree(self.root)

    def _command(self, *args: str) -> list[str]:
        if not self._open:
            raise SdkAuthError("SuiteCloud SDK session is not open")
        return [
            str(self._java),
            f"-Duser.home={self.root}",
            f"-Djava.io.tmpdir={self.root}",
            "-XX:+DisableAttachMechanism",
            "-XX:-UsePerfData",
            "-DintegrationMode",
            "--add-opens",
            "java.base/java.io=ALL-UNNAMED",
            "-jar",
            str(self._jar),
            *args,
        ]

    async def verify_empty_store(self) -> None:
        """Offline runtime diagnostic, only before any authentication attempt."""
        if self._attempted:
            raise SdkAuthError("SuiteCloud SDK authentication already attempted")
        raw = await _run_process(
            self._command("manageauth", "-list"),
            cwd=self.root,
            env=self._env,
            timeout=30,
        )
        receipt = self._receipt(raw)
        if receipt.get("data") != {}:
            raise SdkAuthError("SuiteCloud SDK isolated credential store is not empty")

    @staticmethod
    def _receipt(raw: bytes) -> dict:
        try:
            receipt = json.loads(raw)
        except (ValueError, UnicodeError):
            raise SdkAuthError("SuiteCloud SDK returned an invalid authentication receipt") from None
        if not isinstance(receipt, dict) or receipt.get("status") != "SUCCESS" or receipt.get("errorMessages"):
            raise SdkAuthError("SuiteCloud SDK did not verify authentication")
        return receipt

    async def authenticate(
        self,
        *,
        account_id: str,
        role_id: str,
        certificate_id: str,
        private_key: bytes,
    ) -> VerifiedIdentity:
        if not self._open:
            raise SdkAuthError("SuiteCloud SDK session is not open")
        if self._attempted:
            raise SdkAuthError("SuiteCloud SDK authentication already attempted; reconcile before retry")
        self._attempted = True
        account = account_id.strip().lower().replace("_", "-")
        if not _SANDBOX_ACCOUNT.fullmatch(account) or not _ROLE.fullmatch(role_id):
            raise SdkAuthError("SuiteCloud SDK requires an explicit sandbox account and numeric role")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", certificate_id, re.ASCII):
            raise SdkAuthError("SuiteCloud SDK requires a certificate identity")
        try:
            key = serialization.load_pem_private_key(private_key, password=None)
            if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
                raise ValueError()
        except (ValueError, TypeError):
            raise SdkAuthError("SuiteCloud SDK requires an unencrypted RSA private key of at least 2048 bits") from None
        public_key_sha = hashlib.sha256(
            key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).hexdigest()
        # Check context before writing any secret. Workspace files are never cwd.
        cmd = self._command(
            "authenticateci",
            "-account",
            account,
            "-authid",
            self.auth_id,
            "-certificateid",
            certificate_id,
            "-privatekeypath",
            str(self.root / "private-key.pem"),
        )
        key_file = self.root / "private-key.pem"
        try:
            with open(key_file, "xb", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
                output.write(private_key)
            try:
                raw = await _run_process(cmd, cwd=self.root, env=self._env, timeout=90)
            except asyncio.TimeoutError:
                raise SdkAuthError("SuiteCloud SDK authentication timed out; reconcile before retry") from None
            receipt = self._receipt(raw)
            data = receipt.get("data")
            info = data.get("accountInfo") if isinstance(data, dict) else None
            if not isinstance(info, dict):
                raise SdkAuthError("SuiteCloud SDK did not return verified account and role")
            actual_account, actual_role = info.get("companyId"), info.get("roleId")
            if (
                not isinstance(actual_account, str)
                or not isinstance(actual_role, str)
                or actual_account.strip().lower().replace("_", "-") != account
                or actual_role != role_id
            ):
                raise SdkAuthError("SuiteCloud SDK account or role differs from the authorized identity")
            self._verified_identity = VerifiedIdentity(account, role_id, certificate_id, public_key_sha)
            return self._verified_identity
        finally:
            key_file.unlink(missing_ok=True)

    async def validate_project(
        self,
        *,
        expected_identity: VerifiedIdentity,
        files: dict[str, bytes],
        manifest: list[dict],
        expected_snapshot_sha: str,
        expected_manifest_sha: str,
        project_format: str,
    ) -> SdkValidationReceipt:
        """Inactive fixed server validation; no arbitrary argv, hooks or target.

        A session's successful authentication is required, but is not application
        authorization. Future integration must still enforce current company,
        actor, connection, checks, approval and durable exclusive-claim gates.
        A receipt binds local request bytes and the SDK's success envelope; it
        does not prove deployment, remote bytes, health or rollback. No retry is
        allowed, including after an uncertain result or concurrent invocation.
        """
        if not self._open:
            raise SdkAuthError("SuiteCloud SDK session is not open")
        if self._validation_attempted:
            raise SdkAuthError("SuiteCloud SDK validation already attempted; reconcile before retry")
        self._validation_attempted = True
        if self._verified_identity is None or expected_identity != self._verified_identity:
            raise SdkAuthError("SuiteCloud SDK validation requires matching verified authentication")
        identity = self._verified_identity
        try:
            project = build_ready_reviewed_project(
                files=files,
                manifest=manifest,
                expected_snapshot_sha=expected_snapshot_sha,
                expected_manifest_sha=expected_manifest_sha,
                project_format=project_format,
            )
            async with _project_directory(project) as root:
                cmd = self._command(
                    "validate",
                    "-authid",
                    self.auth_id,
                    "-project",
                    str(root),
                    "-server",
                    "-accountspecificvalues",
                    "ERROR",
                )
                try:
                    raw = await _run_process(cmd, cwd=self.root, env=self._env, timeout=90)
                except TimeoutError:
                    raise SdkAuthError("SuiteCloud SDK validation timed out; reconcile before retry") from None
                self._receipt(raw)
                verification = asyncio.create_task(asyncio.to_thread(_verify_project_bytes, root, project))
                try:
                    await asyncio.shield(verification)
                finally:
                    await _settle_owned_task(verification)
                if not self._open or self._verified_identity != identity:
                    raise SdkAuthError("SuiteCloud SDK validation session is no longer open")
        except (TrustedProjectError, OSError):
            raise SdkAuthError("SuiteCloud SDK validation requires an exact supported reviewed project") from None
        return SdkValidationReceipt(
            identity=identity,
            source_snapshot_sha=expected_snapshot_sha,
            source_manifest_sha=expected_manifest_sha,
            project_format=project_format,
            projected_snapshot_sha=project.snapshot_sha,
            projected_manifest_sha=project.manifest_sha,
            project_sha=project.project_sha,
            sdk_output_sha256=hashlib.sha256(raw).hexdigest(),
        )
