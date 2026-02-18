"""Runner service — execute SDF validate and Jest unit tests in isolated sandboxes."""

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
    "sdf_validate": {
        "cmd": ["sdf", "validate"],
        "timeout": 60,
    },
    "jest_unit_test": {
        "cmd": ["npx", "jest", "--json", "--coverage"],
        "timeout": 120,
    },
}

MAX_ARTIFACT_BYTES = 256 * 1024
TRUNCATED_SUFFIX = "\n...[TRUNCATED]"
AUTH_BEARER_PATTERN = re.compile(r"(?i)\b(authorization:\s*bearer)\s+[A-Za-z0-9._\-+/=]+")
BEARER_PATTERN = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]+")
KEY_VALUE_SECRET_PATTERN = re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*([^\s,;]+)")


class CommandNotAllowedError(Exception):
    """Raised when a run_type is not in the allowlist."""


def validate_run_type(run_type: str) -> dict:
    """Validate run_type against allowlist. Returns command config."""
    if run_type not in ALLOWED_COMMANDS:
        raise CommandNotAllowedError(f"Invalid run_type: {run_type!r}. Allowed: {list(ALLOWED_COMMANDS.keys())}")
    return ALLOWED_COMMANDS[run_type]


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


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
        if patch.baseline_sha256 and _sha256(original_content) != patch.baseline_sha256:
            raise ValueError(f"Patch baseline hash mismatch for {path}")

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


async def execute_run(db: AsyncSession, run_id: uuid.UUID, tenant_id: uuid.UUID) -> WorkspaceRun:
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
    tmp_dir = tempfile.mkdtemp(prefix=f"workspace_run_{run.tenant_id}_")
    start_time = time.monotonic()

    try:
        file_count = await _materialize_snapshot(db, run, tmp_dir)
        exit_code, stdout, stderr = await _run_subprocess(cmd_config["cmd"], tmp_dir, cmd_config["timeout"])
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
    """Get a single run by ID."""
    result = await db.execute(
        select(WorkspaceRun).where(WorkspaceRun.id == run_id, WorkspaceRun.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


async def list_runs(db: AsyncSession, workspace_id: uuid.UUID, tenant_id: uuid.UUID) -> list[WorkspaceRun]:
    """List all runs for a workspace."""
    result = await db.execute(
        select(WorkspaceRun)
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
