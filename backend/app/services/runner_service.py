"""Runner service — execute SDF validate, Jest unit tests, SuiteQL assertions, and sandbox deploy."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import (
    WorkspaceArtifact,
    WorkspaceChangeSet,
    WorkspaceFile,
    WorkspacePatch,
    WorkspaceRun,
)
from app.services import audit_service
from app.services import workspace_service as ws_svc

logger = structlog.get_logger()

# --- Command Allowlist ---

ALLOWED_COMMANDS: dict[str, dict] = {
    "suitecloud_validate": {
        "cmd": ["suitecloud", "project:validate", "--server"],
        "timeout": 180,
    },
    "jest_unit_test": {
        "cmd": ["npx", "jest", "--json", "--coverage"],
        "timeout": 120,
    },
    "suiteql_assertions": {
        "cmd": [],  # No subprocess — executed via SuiteQL client
        "timeout": 300,
    },
    "deploy_sandbox": {
        "cmd": ["suitecloud", "project:deploy", "--destinationFolder", "/SuiteScripts"],
        "timeout": 600,
    },
}

MAX_ARTIFACT_BYTES = 256 * 1024
TRUNCATED_SUFFIX = "\n...[TRUNCATED]"
AUTH_BEARER_PATTERN = re.compile(r"(?i)\b(authorization:\s*bearer)\s+[A-Za-z0-9._\-+/=]+")
BEARER_PATTERN = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]+")
KEY_VALUE_SECRET_PATTERN = re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*([^\s,;]+)")
PRODUCTION_TARGET_PATTERN = re.compile(r"(?i)\b(prod|production|live)\b")
SANDBOX_HINT_PATTERN = re.compile(r"(?i)(?:^|[-_])(sb\d*|sandbox\d*)$")
# Matches `suitecloud project:validate --server`'s terminal FAILURE: line.
# Used by _execute_validate_run as a belt-and-suspenders gate check: if the
# CLI reports validation failed but no diagnostic lines matched _LINE_RE
# (e.g., new failure shape, stderr-only diagnostics), block the gate anyway.
_TERMINAL_FAILURE_RE = re.compile(r"^FAILURE:", re.MULTILINE)


class CommandNotAllowedError(Exception):
    """Raised when a run_type is not in the allowlist."""


class StalePatchError(ValueError):
    """Raised when a patch's baseline_sha256 no longer matches the live file.

    Subclasses ValueError for backward compatibility — older callers that
    caught the bare ValueError raised by `_apply_changeset_overlay` will
    still catch this. The runner specifically catches `StalePatchError`
    first so it can persist `error_category="STALE_PATCH"` and an actionable
    error_message instead of the generic INTERNAL_ERROR.

    Carries enough diagnostic detail (file_path, expected_sha, actual_sha)
    for the frontend to surface a "re-create patch" workflow.
    """

    def __init__(self, file_path: str, expected_sha: str, actual_sha: str):
        self.file_path = file_path
        self.expected_sha = expected_sha
        self.actual_sha = actual_sha
        super().__init__(
            f"Patch baseline hash mismatch for {file_path} (expected {expected_sha[:8]}…, got {actual_sha[:8]}…)"
        )


def validate_run_type(run_type: str) -> dict:
    """Validate run_type against allowlist. Returns command config."""
    if run_type not in ALLOWED_COMMANDS:
        raise CommandNotAllowedError(f"Invalid run_type: {run_type!r}. Allowed: {list(ALLOWED_COMMANDS.keys())}")
    return ALLOWED_COMMANDS[run_type]


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _compute_snapshot_hash(
    *,
    workspace_id: uuid.UUID,
    changeset_id: uuid.UUID | None,
    file_count: int,
    cli_version: str,
    validator_engine: str,
    account_id: str,
) -> str:
    """SHA-256 fingerprint over the inputs that determine validate result identity.

    Used by the orchestrator to short-circuit redundant auto-revalidation when
    nothing about the snapshot or validator has changed.
    """
    payload = f"{workspace_id}:{changeset_id or ''}:{file_count}:{cli_version}:{validator_engine}:{account_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redact_sensitive_output(content: str) -> str:
    redacted = AUTH_BEARER_PATTERN.sub(r"\1 ***REDACTED***", content)
    redacted = BEARER_PATTERN.sub(r"\1 ***REDACTED***", redacted)
    redacted = KEY_VALUE_SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=***REDACTED***", redacted)
    return redacted


def _cap_output(content: str, max_bytes: int = MAX_ARTIFACT_BYTES) -> tuple[str, bool]:
    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content, False
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated + TRUNCATED_SUFFIX, True


def _sanitize_artifact_content(content: str) -> tuple[str, bool]:
    redacted = _redact_sensitive_output(content)
    return _cap_output(redacted)


def _safe_target_path(base_dir: Path, relative_path: str) -> Path:
    normalized = ws_svc.validate_path(relative_path)
    candidate = (base_dir / normalized).resolve()
    if not candidate.is_relative_to(base_dir):
        raise ValueError(f"Path traversal detected while materializing snapshot: {relative_path}")
    return candidate


def _validate_sandbox_target(sandbox_id: str) -> str:
    target = sandbox_id.strip()
    if not target:
        raise ValueError("sandbox_id is required for sandbox deploy")

    lowered = target.lower()
    if PRODUCTION_TARGET_PATTERN.search(lowered):
        raise ValueError("Production deploy targets are disabled")

    # Explicit sandbox markers only: *-sb1, *_sb1, sb1, sandbox1, TSTDRV*
    if not (SANDBOX_HINT_PATTERN.search(lowered) or lowered.startswith("tstdrv")):
        raise ValueError("sandbox_id must clearly reference a sandbox target (example: 6738075-sb1)")

    return target


def _build_deploy_command(base_cmd: list[str], sandbox_id: str) -> list[str]:
    target = _validate_sandbox_target(sandbox_id)
    return [*base_cmd, "--account", target]


async def create_run(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    run_type: str,
    triggered_by: uuid.UUID,
    changeset_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> WorkspaceRun:
    """Create a new workspace run in queued status."""
    cmd_config = validate_run_type(run_type)
    correlation_id = correlation_id or str(uuid.uuid4())

    run = WorkspaceRun(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        changeset_id=changeset_id,
        run_type=run_type,
        status="queued",
        triggered_by=triggered_by,
        correlation_id=correlation_id,
        command=" ".join(cmd_config["cmd"]),
    )
    db.add(run)
    await db.flush()
    return run


async def _load_workspace_snapshot(db: AsyncSession, run: WorkspaceRun) -> dict[str, str]:
    result = await db.execute(
        select(WorkspaceFile).where(
            WorkspaceFile.workspace_id == run.workspace_id,
            WorkspaceFile.tenant_id == run.tenant_id,
            WorkspaceFile.is_directory.is_(False),
        )
    )
    files = {f.path: f.content or "" for f in result.scalars().all()}
    return files


async def _apply_changeset_overlay(db: AsyncSession, run: WorkspaceRun, files: dict[str, str]) -> dict[str, str]:
    if run.changeset_id is None:
        return files

    cs_result = await db.execute(
        select(WorkspaceChangeSet).where(
            WorkspaceChangeSet.id == run.changeset_id,
            WorkspaceChangeSet.workspace_id == run.workspace_id,
            WorkspaceChangeSet.tenant_id == run.tenant_id,
        )
    )
    changeset = cs_result.scalar_one_or_none()
    if changeset is None:
        raise ValueError("Changeset not found for run materialization")
    if changeset.status == "applied":
        # Patches already committed to workspace_files; snapshot is correct as-is.
        # Auto-validate runs queued after apply_patch land here — preserve the
        # changeset_id linkage for audit but skip re-applying the diffs.
        logger.debug(
            "runner.overlay_skipped_applied",
            changeset_id=str(run.changeset_id),
            run_id=str(run.id),
        )
        return files
    if changeset.status != "approved":
        raise ValueError(f"Changeset must be approved before run execution (current: {changeset.status})")

    patch_result = await db.execute(
        select(WorkspacePatch)
        .where(
            WorkspacePatch.changeset_id == run.changeset_id,
            WorkspacePatch.tenant_id == run.tenant_id,
        )
        .order_by(WorkspacePatch.apply_order)
    )
    patches = list(patch_result.scalars().all())

    for patch in patches:
        path = ws_svc.validate_path(patch.file_path)
        if patch.operation == "create":
            files[path] = patch.new_content or ""
            continue
        if patch.operation == "delete":
            files.pop(path, None)
            continue
        if patch.operation != "modify":
            raise ValueError(f"Unsupported patch operation for runner snapshot: {patch.operation}")

        if path not in files:
            raise ValueError(f"Patch modify target does not exist in workspace snapshot: {path}")

        original_content = files[path]
        if patch.baseline_sha256:
            actual_sha = _sha256(original_content)
            if actual_sha != patch.baseline_sha256:
                raise StalePatchError(
                    file_path=path,
                    expected_sha=patch.baseline_sha256,
                    actual_sha=actual_sha,
                )

        if patch.unified_diff:
            files[path] = ws_svc._apply_diff(original_content, patch.unified_diff)
        elif patch.new_content is not None:
            files[path] = patch.new_content
        else:
            raise ValueError(f"Modify patch has no diff/content for {path}")

    return files


async def _materialize_snapshot(db: AsyncSession, run: WorkspaceRun, tmp_dir: str) -> int:
    files = await _load_workspace_snapshot(db, run)
    files = await _apply_changeset_overlay(db, run, files)

    base_dir = Path(tmp_dir).resolve()
    for path, content in files.items():
        target = _safe_target_path(base_dir, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return len(files)


def _create_artifact(
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    artifact_type: str,
    content: str,
) -> WorkspaceArtifact:
    """Create an immutable artifact record."""
    content_bytes = content.encode("utf-8")
    return WorkspaceArtifact(
        tenant_id=tenant_id,
        run_id=run_id,
        artifact_type=artifact_type,
        content=content,
        size_bytes=len(content_bytes),
        sha256_hash=hashlib.sha256(content_bytes).hexdigest(),
    )


async def _store_artifact(
    db: AsyncSession,
    run: WorkspaceRun,
    artifact_type: str,
    content: str,
) -> WorkspaceArtifact:
    sanitized, truncated = _sanitize_artifact_content(content)
    artifact = _create_artifact(run.tenant_id, run.id, artifact_type, sanitized)
    db.add(artifact)
    await db.flush()
    await audit_service.log_event(
        db=db,
        tenant_id=run.tenant_id,
        category="workspace",
        action="artifact_created",
        actor_id=run.triggered_by,
        resource_type="workspace_artifact",
        resource_id=str(artifact.id),
        correlation_id=run.correlation_id,
        payload={
            "run_id": str(run.id),
            "artifact_type": artifact_type,
            "size_bytes": artifact.size_bytes,
            "sha256_hash": artifact.sha256_hash,
            "truncated": truncated,
        },
    )
    return artifact


def _parse_json_output(raw_output: str) -> dict[str, Any] | None:
    stripped = raw_output.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(stripped[start : end + 1])
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    except json.JSONDecodeError:
        return None


async def _run_subprocess(cmd: list[str], cwd: str, timeout: int) -> tuple[int, str, str]:
    """Run a subprocess and return (exit_code, stdout, stderr).

    This is extracted to allow mocking in tests.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": cwd,
        "TMPDIR": cwd,
    }
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return (
        proc.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


async def _audit_run_event(
    db: AsyncSession,
    run: WorkspaceRun,
    action: str,
    status: str,
    payload: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> None:
    await audit_service.log_event(
        db=db,
        tenant_id=run.tenant_id,
        category="workspace",
        action=action,
        actor_id=run.triggered_by,
        resource_type="workspace_run",
        resource_id=str(run.id),
        correlation_id=run.correlation_id,
        payload=payload,
        status=status,
        error_message=error_message,
    )


async def _execute_validate_run(
    db: AsyncSession,
    run: WorkspaceRun,
    extra_params: dict[str, Any],
) -> WorkspaceRun:
    """Execute a ``suitecloud project:validate --server`` run.

    Flow:
      1. Materialize the workspace snapshot (with optional changeset overlay).
      2. Seed the CLI credential file from the active NetSuite connection.
         If the seed fails, abort early with ``gate_status=block`` and a
         clear ``stderr`` artifact (no subprocess is launched).
      3. Run the CLI via ``_run_subprocess`` (which sets HOME=cwd so the
         seeded credential file is discovered).
      4. Parse stdout into ``ValidationHit`` rows.
      5. Update the run record with ``has_errors``, ``has_warnings``,
         ``gate_status``, ``parser_version``, ``validator_engine``, and
         ``snapshot_hash``. Gating is derived from PARSED output, NOT from
         the raw exit code.
    """
    # Local imports keep test patches against the module attributes effective
    # (the auth seeder + parser are mocked in test_validate_runner_integration).
    from app.models.workspace import ValidationHit
    from app.services.workspace.suitecloud_auth_seeder import (
        AuthSeederError,
        seed_credentials_for_run,
    )
    from app.services.workspace.validate_parser import (
        PARSER_VERSION,
        parse_suitecloud_validate_output,
    )

    cmd_config = ALLOWED_COMMANDS["suitecloud_validate"]
    tmp_dir = tempfile.mkdtemp(prefix=f"workspace_validate_{run.tenant_id}_")
    start_time = time.monotonic()

    try:
        file_count = await _materialize_snapshot(db, run, tmp_dir)

        # Seed CLI credentials. If this fails, abort early with a clear stderr
        # artifact and a blocking gate status. The seeder also returns the
        # resolved account_id (used below for the snapshot hash) so we avoid a
        # redundant Connection lookup + decrypt — which had a silent-corruption
        # path (account_id="unknown" → snapshot_hash collisions) if the row
        # vanished mid-run.
        try:
            seeded = await seed_credentials_for_run(
                db=db,
                tenant_id=run.tenant_id,
                auth_root=Path(tmp_dir),
                project_id=str(run.workspace_id),
            )
        except AuthSeederError as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            run.status = "failed"
            run.exit_code = -1
            run.gate_status = "block"
            run.has_errors = True
            run.validator_engine = "suitecloud_server"
            run.completed_at = datetime.now(timezone.utc)
            run.duration_ms = duration_ms
            await _store_artifact(db, run, "stderr", f"auth_required: {exc}")
            await _audit_run_event(
                db,
                run,
                action="run_failed",
                status="error",
                payload={
                    "run_type": run.run_type,
                    "error_category": "AUTH_REQUIRED",
                    "duration_ms": duration_ms,
                },
                error_message=str(exc),
            )
            await db.flush()
            return run

        account_id = seeded.account_id

        # Run the CLI. ``_run_subprocess`` sets HOME=cwd=tmp_dir, so the
        # seeded credential file at ``$HOME/.suitecloud-sdk/credentials/...``
        # is discoverable.
        try:
            exit_code, stdout, stderr = await _run_subprocess(cmd_config["cmd"], tmp_dir, cmd_config["timeout"])
        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            run.status = "failed"
            run.exit_code = -2
            run.gate_status = "block"
            run.has_errors = True
            run.validator_engine = "suitecloud_server"
            run.completed_at = datetime.now(timezone.utc)
            run.duration_ms = duration_ms
            await _store_artifact(
                db,
                run,
                "stderr",
                f"timeout: suitecloud project:validate --server exceeded {cmd_config['timeout']}s",
            )
            await _audit_run_event(
                db,
                run,
                action="run_failed",
                status="error",
                payload={
                    "run_type": run.run_type,
                    "error_category": "TIMEOUT",
                    "duration_ms": duration_ms,
                },
                error_message="timeout",
            )
            await db.flush()
            return run

        duration_ms = int((time.monotonic() - start_time) * 1000)
        parsed = parse_suitecloud_validate_output(stdout)

        # Persist raw stdout/stderr always — the parser is best-effort and
        # the raw artifact is the fallback for inspection / parser_error hits.
        if stdout:
            await _store_artifact(db, run, "stdout", stdout)
        if stderr:
            await _store_artifact(db, run, "stderr", stderr)

        # Persist hits. ``rule_id`` is left NULL here — Task 10 (agent
        # narration helper) populates Oracle rule IDs as a follow-up.
        for parsed_hit in parsed.hits:
            db.add(
                ValidationHit(
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    file_path=parsed_hit.file_path,
                    line=parsed_hit.line,
                    severity=parsed_hit.severity,
                    code=parsed_hit.code,
                    rule_id=None,
                    message=parsed_hit.message,
                    fingerprint=parsed_hit.fingerprint,
                )
            )

        # Run-record updates. Gating derives from PARSED output, NOT exit_code
        # (codex #11 — exit code is recorded but never load-bearing).
        #
        # Block the gate when ANY of these is true:
        #   - parser extracted at least one severity="error" hit
        #   - parser fell back to a synthetic parser_error hit (CLI output was
        #     unparseable; we cannot prove the validate passed)
        #   - the CLI emitted a terminal FAILURE: line but no error lines
        #     matched the diagnostic regex (e.g., diagnostics on stderr only,
        #     or new FAILURE shapes the regex doesn't cover)
        # Each of these means "we cannot trust this as a clean validate."
        has_parser_error = any(h.severity == "parser_error" for h in parsed.hits)
        has_terminal_failure = bool(_TERMINAL_FAILURE_RE.search(stdout))
        gate_block = parsed.has_errors or has_parser_error or has_terminal_failure

        run.exit_code = exit_code
        run.has_errors = parsed.has_errors or has_parser_error or has_terminal_failure
        run.has_warnings = parsed.has_warnings
        run.parser_version = PARSER_VERSION
        run.validator_engine = "suitecloud_server"
        if gate_block:
            run.status = "failed"
            run.gate_status = "block"
        else:
            run.status = "passed"
            run.gate_status = "pass"
        run.snapshot_hash = _compute_snapshot_hash(
            workspace_id=run.workspace_id,
            changeset_id=run.changeset_id,
            file_count=file_count,
            cli_version="suitecloud-cli@latest",
            validator_engine="suitecloud_server",
            account_id=account_id,
        )
        run.completed_at = datetime.now(timezone.utc)
        run.duration_ms = duration_ms

        result_payload = {
            "run_id": str(run.id),
            "run_type": run.run_type,
            "status": run.status,
            "gate_status": run.gate_status,
            "has_errors": run.has_errors,
            "has_warnings": run.has_warnings,
            "hit_count": len(parsed.hits),
            "duration_ms": duration_ms,
            "parser_version": PARSER_VERSION,
            "validator_engine": "suitecloud_server",
        }
        await _store_artifact(db, run, "result_json", json.dumps(result_payload, default=str))
        await _audit_run_event(
            db,
            run,
            action="run_succeeded" if run.status == "passed" else "run_failed",
            status="success" if run.status == "passed" else "error",
            payload={
                **result_payload,
                "artifact_count": (1 if stdout else 0) + (1 if stderr else 0) + 1,
            },
            error_message=None if run.status == "passed" else f"{len(parsed.hits)} validation hit(s)",
        )
        await db.flush()
        return run
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _execute_assertions_run(
    db: AsyncSession,
    run: WorkspaceRun,
    extra_params: dict[str, Any],
) -> WorkspaceRun:
    """Execute SuiteQL assertions run (no subprocess — uses assertion service)."""
    from app.services import assertion_service

    start_time = time.monotonic()
    assertions = extra_params.get("assertions", [])

    if not assertions:
        run.status = "error"
        run.completed_at = datetime.now(timezone.utc)
        run.duration_ms = int((time.monotonic() - start_time) * 1000)
        await _store_artifact(db, run, "stderr", "No assertions provided")
        await _audit_run_event(db, run, action="run_failed", status="error", error_message="No assertions provided")
        await db.flush()
        return run

    # Create a stub SuiteQL executor that uses the MCP netsuite.suiteql tool
    async def suiteql_executor(query: str, limit: int, timeout: int) -> dict:
        from app.mcp.tools.netsuite_suiteql import execute as suiteql_execute

        context = {"tenant_id": str(run.tenant_id), "db": db}
        result = await suiteql_execute({"query": query, "limit": limit}, context=context)
        return result

    try:
        report = await assertion_service.execute_assertions(
            db=db,
            tenant_id=run.tenant_id,
            run_id=run.id,
            assertions=assertions,
            suiteql_executor=suiteql_executor,
            correlation_id=run.correlation_id,
            actor_id=run.triggered_by,
        )
    except Exception as exc:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        run.status = "error"
        run.completed_at = datetime.now(timezone.utc)
        run.duration_ms = duration_ms
        await _store_artifact(db, run, "stderr", f"Assertion execution error: {exc}")
        await _audit_run_event(db, run, action="run_failed", status="error", error_message=str(exc))
        await db.flush()
        return run

    duration_ms = int((time.monotonic() - start_time) * 1000)
    run.status = "passed" if report["overall_status"] == "passed" else "failed"
    run.completed_at = datetime.now(timezone.utc)
    run.duration_ms = duration_ms
    run.exit_code = 0 if run.status == "passed" else 1

    # Store assertion report as artifact
    await _store_artifact(db, run, "suiteql_report", json.dumps(report, default=str))
    await _store_artifact(
        db,
        run,
        "result_json",
        json.dumps(
            {
                "run_id": str(run.id),
                "run_type": run.run_type,
                "status": run.status,
                "summary": report["summary"],
                "duration_ms": duration_ms,
            },
            default=str,
        ),
    )

    await _audit_run_event(
        db,
        run,
        action="run_succeeded" if run.status == "passed" else "run_failed",
        status="success" if run.status == "passed" else "error",
        payload={
            "run_type": run.run_type,
            "summary": report["summary"],
            "duration_ms": duration_ms,
        },
    )
    await db.flush()
    return run


async def execute_run(
    db: AsyncSession,
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    extra_params: dict[str, Any] | None = None,
) -> WorkspaceRun:
    """Execute a workspace run: materialize snapshot, execute, and store immutable artifacts."""
    result = await db.execute(
        select(WorkspaceRun).where(WorkspaceRun.id == run_id, WorkspaceRun.tenant_id == tenant_id)
    )
    run = result.scalar_one()

    run.status = "running"
    run.started_at = datetime.now(timezone.utc)
    await db.flush()
    await _audit_run_event(
        db,
        run,
        action="run_started",
        status="success",
        payload={"run_type": run.run_type, "workspace_id": str(run.workspace_id)},
    )

    cmd_config = validate_run_type(run.run_type)

    # --- suitecloud_validate: seed CLI creds, run subprocess, parse hits ---
    if run.run_type == "suitecloud_validate":
        return await _execute_validate_run(db, run, extra_params or {})

    # --- SuiteQL assertions: no subprocess, use assertion service ---
    if run.run_type == "suiteql_assertions":
        return await _execute_assertions_run(db, run, extra_params or {})

    tmp_dir = tempfile.mkdtemp(prefix=f"workspace_run_{run.tenant_id}_")
    start_time = time.monotonic()

    try:
        file_count = await _materialize_snapshot(db, run, tmp_dir)

        cmd = cmd_config["cmd"]
        if run.run_type == "deploy_sandbox":
            sandbox_id = (extra_params or {}).get("sandbox_id")
            cmd = _build_deploy_command(cmd_config["cmd"], str(sandbox_id or ""))
            run.command = " ".join(cmd)
            await db.flush()

        exit_code, stdout, stderr = await _run_subprocess(cmd, tmp_dir, cmd_config["timeout"])
        duration_ms = int((time.monotonic() - start_time) * 1000)

        run.exit_code = exit_code
        run.status = "passed" if exit_code == 0 else "failed"
        run.completed_at = datetime.now(timezone.utc)
        run.duration_ms = duration_ms

        artifact_count = 0
        if stdout:
            await _store_artifact(db, run, "stdout", stdout)
            artifact_count += 1
        if stderr:
            await _store_artifact(db, run, "stderr", stderr)
            artifact_count += 1

        if run.run_type == "jest_unit_test":
            report = _parse_json_output(stdout)
            if report is not None:
                await _store_artifact(db, run, "report_json", json.dumps(report, default=str))
                artifact_count += 1

            coverage_path = Path(tmp_dir) / "coverage" / "coverage-summary.json"
            if coverage_path.exists() and coverage_path.is_file():
                coverage_raw = coverage_path.read_text(encoding="utf-8", errors="replace")
                await _store_artifact(db, run, "coverage_json", coverage_raw)
                artifact_count += 1

        result_payload = {
            "run_id": str(run.id),
            "run_type": run.run_type,
            "status": run.status,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "materialized_file_count": file_count,
        }
        await _store_artifact(db, run, "result_json", json.dumps(result_payload, default=str))
        artifact_count += 1

        await _audit_run_event(
            db,
            run,
            action="run_succeeded" if run.status == "passed" else "run_failed",
            status="success" if run.status == "passed" else "error",
            payload={**result_payload, "artifact_count": artifact_count},
            error_message=None if run.status == "passed" else "Command exited non-zero",
        )
    except StalePatchError as exc:
        # File drifted after the patch was proposed. Surface a STALE_PATCH
        # category with an actionable message so the frontend can guide the
        # user to re-create the patch instead of showing INTERNAL_ERROR.
        logger.warning(
            "runner.stale_patch",
            run_id=str(run_id),
            file_path=exc.file_path,
            expected_sha=exc.expected_sha[:12],
            actual_sha=exc.actual_sha[:12],
        )
        duration_ms = int((time.monotonic() - start_time) * 1000)
        run.status = "error"
        run.completed_at = datetime.now(timezone.utc)
        run.duration_ms = duration_ms

        actionable = (
            f"The file '{exc.file_path}' has changed since this patch was created, "
            "so the patch is stale and can't be applied cleanly. Re-create the patch "
            "by asking the agent to redo the change against the current file content."
        )
        await _store_artifact(db, run, "stderr", actionable)
        await _store_artifact(
            db,
            run,
            "result_json",
            json.dumps(
                {
                    "run_id": str(run.id),
                    "run_type": run.run_type,
                    "status": run.status,
                    "error_category": "STALE_PATCH",
                    "error_message": actionable,
                    "stale_patch": {
                        "file_path": exc.file_path,
                        "expected_sha": exc.expected_sha,
                        "actual_sha": exc.actual_sha,
                    },
                    "duration_ms": duration_ms,
                }
            ),
        )
        await _audit_run_event(
            db,
            run,
            action="run_failed",
            status="error",
            payload={
                "run_type": run.run_type,
                "error_category": "STALE_PATCH",
                "file_path": exc.file_path,
                "duration_ms": duration_ms,
            },
            error_message=actionable,
        )
    except asyncio.TimeoutError:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        run.status = "error"
        run.completed_at = datetime.now(timezone.utc)
        run.duration_ms = duration_ms

        timeout_error = f"Command timed out after {cmd_config['timeout']}s"
        await _store_artifact(db, run, "stderr", timeout_error)
        await _store_artifact(
            db,
            run,
            "result_json",
            json.dumps(
                {
                    "run_id": str(run.id),
                    "run_type": run.run_type,
                    "status": run.status,
                    "error_category": "TIMEOUT",
                    "error_message": timeout_error,
                    "duration_ms": duration_ms,
                }
            ),
        )
        await _audit_run_event(
            db,
            run,
            action="run_failed",
            status="error",
            payload={"run_type": run.run_type, "error_category": "TIMEOUT", "duration_ms": duration_ms},
            error_message=timeout_error,
        )
    except Exception as exc:
        logger.error("runner.execute_failed", run_id=str(run_id), error=str(exc))
        duration_ms = int((time.monotonic() - start_time) * 1000)
        run.status = "error"
        run.completed_at = datetime.now(timezone.utc)
        run.duration_ms = duration_ms

        execution_error = f"Execution error: {exc}"
        await _store_artifact(db, run, "stderr", execution_error)
        await _store_artifact(
            db,
            run,
            "result_json",
            json.dumps(
                {
                    "run_id": str(run.id),
                    "run_type": run.run_type,
                    "status": run.status,
                    "error_category": "INTERNAL_ERROR",
                    "error_message": str(exc),
                    "duration_ms": duration_ms,
                }
            ),
        )
        await _audit_run_event(
            db,
            run,
            action="run_failed",
            status="error",
            payload={"run_type": run.run_type, "error_category": "INTERNAL_ERROR", "duration_ms": duration_ms},
            error_message=str(exc),
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    await db.flush()
    return run


async def get_run(db: AsyncSession, run_id: uuid.UUID, tenant_id: uuid.UUID) -> WorkspaceRun | None:
    """Get a single run by ID with its validation_hits eager-loaded."""
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(WorkspaceRun)
        .options(selectinload(WorkspaceRun.validation_hits))
        .where(WorkspaceRun.id == run_id, WorkspaceRun.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


async def list_runs(db: AsyncSession, workspace_id: uuid.UUID, tenant_id: uuid.UUID) -> list[WorkspaceRun]:
    """List all runs for a workspace with validation_hits eager-loaded.

    Eager-loading avoids an N+1 when serializing — the runs panel needs the
    findings for any suitecloud_validate runs in the list.
    """
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(WorkspaceRun)
        .options(selectinload(WorkspaceRun.validation_hits))
        .where(WorkspaceRun.workspace_id == workspace_id, WorkspaceRun.tenant_id == tenant_id)
        .order_by(WorkspaceRun.created_at.desc())
    )
    return list(result.scalars().all())


async def get_artifacts(db: AsyncSession, run_id: uuid.UUID, tenant_id: uuid.UUID) -> list[WorkspaceArtifact]:
    """Get all artifacts for a run."""
    result = await db.execute(
        select(WorkspaceArtifact)
        .where(
            WorkspaceArtifact.run_id == run_id,
            WorkspaceArtifact.tenant_id == tenant_id,
        )
        .order_by(WorkspaceArtifact.created_at.asc())
    )
    return list(result.scalars().all())
