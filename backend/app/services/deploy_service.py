"""Deploy Service — gated sandbox deploy with prerequisite checks."""

from __future__ import annotations

import json
import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import WorkspaceArtifact, WorkspaceRun
from app.services.workspace.suitecloud_auth_seeder import AuthSeederError, require_verified_identity

logger = structlog.get_logger()

# --- Deploy gate errors ---


class DeployGateError(Exception):
    """Raised when a deploy prerequisite is not met."""


class ProductionDeployDisabledError(Exception):
    """Raised when a production deploy is attempted (hard-disabled)."""


# --- Gating checks ---


async def check_deploy_prerequisites(
    db: AsyncSession,
    changeset_id: uuid.UUID,
    tenant_id: uuid.UUID,
    require_assertions: bool = False,
    override_reason: str | None = None,
    expected_snapshot_hash: str | None = None,
    expected_account_id: str | None = None,
    expected_target_identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Check all prerequisites for sandbox deploy.

    Validation and unit-test receipts must match the canonical project content.
    When no expected hash is supplied, compute it from the current approved
    changeset. Legacy receipts without content identity fail closed.
    Validation requires both a passed lifecycle and a passing parser gate.

    Returns a dict with gate status:
    {
        "allowed": bool,
        "gates": {
            "validate": {"status": "passed"|"failed"|"missing"|"stale", "run_id": ..., "fresh": bool},
            "unit_tests": {"status": ..., "run_id": ...},
            "assertions": {"status": ..., "run_id": ..., "skipped": bool},
        },
        "override": {"applied": bool, "reason": str|None},
        "blocked_reason": str|None,
    }
    """
    if expected_snapshot_hash is None:
        # Every caller enforces freshness; omission cannot disable this gate.
        from app.models.workspace import WorkspaceChangeSet
        from app.services.deploy_preview_service import compute_deploy_manifest

        cs = (
            await db.execute(
                select(WorkspaceChangeSet).where(
                    WorkspaceChangeSet.id == changeset_id, WorkspaceChangeSet.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if cs is None or cs.status != "approved":
            return {
                "allowed": False,
                "gates": {},
                "override": {"applied": False, "reason": None},
                "blocked_reason": "Changeset must be approved",
            }
        manifest = await compute_deploy_manifest(
            db=db, changeset_id=changeset_id, tenant_id=tenant_id, workspace_id=cs.workspace_id
        )
        expected_snapshot_hash = manifest["snapshot_sha"]
    gates: dict[str, Any] = {}

    # --- validate gate (suitecloud_validate, with snapshot-hash freshness) ---
    validate_run = await _get_latest_run(db, changeset_id, tenant_id, "suitecloud_validate")
    if validate_run is None:
        gates["validate"] = {"status": "missing", "run_id": None, "fresh": False}
    else:
        is_fresh = validate_run.snapshot_hash == expected_snapshot_hash
        if not is_fresh:
            gates["validate"] = {
                "status": "stale",
                "run_id": str(validate_run.id),
                "fresh": False,
            }
        elif validate_run.status == "passed" and validate_run.gate_status == "pass":
            gates["validate"] = {
                "status": "passed",
                "run_id": str(validate_run.id),
                "fresh": True,
            }
        else:
            # Missing or blocking parser evidence cannot certify validation.
            gates["validate"] = {
                "status": "failed",
                "run_id": str(validate_run.id),
                "fresh": True,
            }

        if expected_account_id is not None and gates["validate"]["status"] == "passed":
            # Content identity is shared with local tests. Server validation also
            # needs evidence for the actual destination account.
            artifact = (
                await db.execute(
                    select(WorkspaceArtifact).where(
                        WorkspaceArtifact.run_id == validate_run.id,
                        WorkspaceArtifact.tenant_id == tenant_id,
                        WorkspaceArtifact.artifact_type == "result_json",
                    )
                )
            ).scalar_one_or_none()
            try:
                receipt = json.loads(artifact.content or "{}") if artifact else {}
                account, role = require_verified_identity(
                    account_id=receipt.get("account_id"),
                    role_id=receipt.get("role_id"),
                    expected_account_id=expected_account_id,
                )
                identity = {"account_id": account, "role_id": role}
                if expected_target_identity is not None and identity != expected_target_identity:
                    raise AuthSeederError("Validation identity changed since preview")
                gates["validate"]["identity"] = identity
            except (ValueError, AttributeError, AuthSeederError):
                gates["validate"]["status"] = "target_mismatch"

    # Check unit tests
    test_run = await _get_latest_run(db, changeset_id, tenant_id, "jest_unit_test")
    if test_run is None:
        gates["unit_tests"] = {"status": "missing", "run_id": None}
    else:
        is_fresh = test_run.snapshot_hash == expected_snapshot_hash
        gates["unit_tests"] = {
            "status": test_run.status if is_fresh else "stale",
            "run_id": str(test_run.id),
            "fresh": is_fresh,
        }

    # Check assertions
    if require_assertions:
        assertion_run = await _get_latest_run(db, changeset_id, tenant_id, "suiteql_assertions")
        if assertion_run is None:
            gates["assertions"] = {"status": "missing", "run_id": None, "skipped": False}
        else:
            gates["assertions"] = {
                "status": assertion_run.status,
                "run_id": str(assertion_run.id),
                "skipped": False,
            }
    else:
        gates["assertions"] = {"status": "not_required", "run_id": None, "skipped": True}

    # Evaluate gates — validate must be both passed AND fresh.
    validate_ok = gates["validate"]["status"] == "passed" and gates["validate"].get("fresh") is True
    tests_ok = gates["unit_tests"]["status"] == "passed" and gates["unit_tests"].get("fresh") is True
    assertions_ok = gates["assertions"]["status"] in ("passed", "not_required")
    override_applied = False
    blocked_reason = None

    # Validate and unit tests are mandatory and cannot be overridden.
    if not validate_ok or not tests_ok:
        failures = []
        if not validate_ok:
            failures.append(f"validate: {gates['validate']['status']}")
        if not tests_ok:
            failures.append(f"unit_tests: {gates['unit_tests']['status']}")
        blocked_reason = f"Deploy blocked — prerequisites not met: {', '.join(failures)}"
        return {
            "allowed": False,
            "gates": gates,
            "override": {"applied": False, "reason": None},
            "blocked_reason": blocked_reason,
        }

    # Assertions can be overridden only when explicitly required.
    if not assertions_ok:
        if override_reason:
            override_applied = True
            logger.warning(
                "deploy.gate_override",
                changeset_id=str(changeset_id),
                tenant_id=str(tenant_id),
                override_reason=override_reason,
                gates=gates,
            )
        else:
            blocked_reason = f"Deploy blocked — prerequisites not met: assertions: {gates['assertions']['status']}"

    return {
        "allowed": assertions_ok or override_applied,
        "gates": gates,
        "override": {"applied": override_applied, "reason": override_reason},
        "blocked_reason": blocked_reason,
    }


async def _get_latest_run(
    db: AsyncSession,
    changeset_id: uuid.UUID,
    tenant_id: uuid.UUID,
    run_type: str,
) -> WorkspaceRun | None:
    """Get the most recent run of a given type for a changeset."""
    result = await db.execute(
        select(WorkspaceRun)
        .where(
            WorkspaceRun.changeset_id == changeset_id,
            WorkspaceRun.tenant_id == tenant_id,
            WorkspaceRun.run_type == run_type,
        )
        .order_by(WorkspaceRun.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_latest_runs_for_changeset(
    db: AsyncSession,
    changeset_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> dict[str, WorkspaceRun | None]:
    """Get the latest run of each type for a changeset."""
    run_types = ["suitecloud_validate", "jest_unit_test", "suiteql_assertions", "deploy_sandbox"]
    runs = {}
    for rt in run_types:
        runs[rt] = await _get_latest_run(db, changeset_id, tenant_id, rt)
    return runs
