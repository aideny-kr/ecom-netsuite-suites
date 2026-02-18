"""Deploy Service — gated sandbox deploy with prerequisite checks."""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import WorkspaceRun

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
) -> dict[str, Any]:
    """Check all prerequisites for sandbox deploy.

    Returns a dict with gate status:
    {
        "allowed": bool,
        "gates": {
            "validate": {"status": "passed"|"failed"|"missing", "run_id": ...},
            "unit_tests": {"status": ..., "run_id": ...},
            "assertions": {"status": ..., "run_id": ..., "skipped": bool},
        },
        "override": {"applied": bool, "reason": str|None},
        "blocked_reason": str|None,
    }
    """
    gates: dict[str, Any] = {}

    # Check validate
    validate_run = await _get_latest_run(db, changeset_id, tenant_id, "sdf_validate")
    if validate_run is None:
        gates["validate"] = {"status": "missing", "run_id": None}
    else:
        gates["validate"] = {"status": validate_run.status, "run_id": str(validate_run.id)}

    # Check unit tests
    test_run = await _get_latest_run(db, changeset_id, tenant_id, "jest_unit_test")
    if test_run is None:
        gates["unit_tests"] = {"status": "missing", "run_id": None}
    else:
        gates["unit_tests"] = {"status": test_run.status, "run_id": str(test_run.id)}

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

    # Evaluate gates
    validate_ok = gates["validate"]["status"] == "passed"
    tests_ok = gates["unit_tests"]["status"] == "passed"
    assertions_ok = gates["assertions"]["status"] in ("passed", "not_required")

    all_passed = validate_ok and tests_ok and assertions_ok

    override_applied = False
    blocked_reason = None

    if not all_passed:
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
            failures = []
            if not validate_ok:
                failures.append(f"validate: {gates['validate']['status']}")
            if not tests_ok:
                failures.append(f"unit_tests: {gates['unit_tests']['status']}")
            if not assertions_ok:
                failures.append(f"assertions: {gates['assertions']['status']}")
            blocked_reason = f"Deploy blocked — prerequisites not met: {', '.join(failures)}"

    return {
        "allowed": all_passed or override_applied,
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
    run_types = ["sdf_validate", "jest_unit_test", "suiteql_assertions", "deploy_sandbox"]
    runs = {}
    for rt in run_types:
        runs[rt] = await _get_latest_run(db, changeset_id, tenant_id, rt)
    return runs
