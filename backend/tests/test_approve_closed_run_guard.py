"""Period-close integrity: the single-approve paths must be a hard freeze.

`close_period` deliberately leaves ``auto_matched`` lines whose ``bucket='needs_review'``
(confident match, *material* variance) unlocked so a human reviews them. But the
single-approve paths had NO ``run.status`` guard — so a line left unlocked in a now-
``closed`` run could still be approved post-close, flipping it to ``status='approved'``
inside a closed period and never re-locked. Close must be a hard freeze: once a run is
``closed``/``locked``, neither the REST single-approve endpoint nor the chat-agent MCP
tool may modify any of its lines.
"""

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.v1.reconciliation import approve_result
from app.mcp.tools.recon_approve import execute
from app.models.audit import AuditEvent
from app.schemas.reconciliation import ReconResultApprove
from app.services.reconciliation.four_bucket_classifier import BUCKET_NEEDS_REVIEW
from tests.conftest import (
    create_test_recon_result,
    create_test_recon_run,
    create_test_user,
)


async def _count_audit(db, *, resource_id: str) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "recon.approve",
                AuditEvent.resource_id == resource_id,
            )
        )
    ).scalar_one()


async def test_approve_result_blocked_when_run_closed(db, tenant_a):
    """REST single-approve must 400 + NOT mutate a line in a closed run."""
    user, _ = await create_test_user(db, tenant_a)
    run = await create_test_recon_run(db, tenant_a.id, status="closed")
    # An auto_matched + needs_review line: left unlocked by close_period.
    result = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="auto_matched",
        bucket=BUCKET_NEEDS_REVIEW,
    )
    await db.flush()

    with pytest.raises(HTTPException) as exc:
        await approve_result(
            str(result.id),
            ReconResultApprove(result_id=str(result.id)),
            user=user,
            db=db,
        )
    assert exc.value.status_code == 400
    assert "closed" in exc.value.detail.lower()

    # Status must be UNCHANGED (still auto_matched), and no approve audit row written.
    await db.refresh(result)
    assert result.status == "auto_matched"
    assert await _count_audit(db, resource_id=str(result.id)) == 0


async def test_approve_result_blocked_when_carried_forward(db, tenant_a):
    """A carried_forward result (an acknowledged timing/reconciling item) must not
    be flippable to 'approved' via the single-approve path — that would let it get
    LOCKED at close, violating the carried_forward-never-locks invariant. The guard
    must key off TERMINAL_RESULT_STATUSES, not a literal ('approved', 'locked') check."""
    user, _ = await create_test_user(db, tenant_a)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    result = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="carried_forward",
    )
    await db.flush()

    with pytest.raises(HTTPException) as exc:
        await approve_result(
            str(result.id),
            ReconResultApprove(result_id=str(result.id)),
            user=user,
            db=db,
        )
    assert exc.value.status_code == 400

    await db.refresh(result)
    assert result.status == "carried_forward"
    assert await _count_audit(db, resource_id=str(result.id)) == 0


async def test_recon_approve_tool_blocked_when_run_closed(db, tenant_a):
    """Chat-agent MCP tool must return failure + NOT mutate / audit a closed-run line."""
    user, _ = await create_test_user(db, tenant_a)
    run = await create_test_recon_run(db, tenant_a.id, status="closed")
    result = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="auto_matched",
        bucket=BUCKET_NEEDS_REVIEW,
    )
    await db.flush()

    out = await execute(
        {"result_id": str(result.id)},
        db=db,
        tenant_id=tenant_a.id,
        user_id=user.id,
    )

    assert out["success"] is False
    assert "closed" in out["error"].lower()

    # Status UNCHANGED + zero new audit rows.
    await db.refresh(result)
    assert result.status == "auto_matched"
    assert await _count_audit(db, resource_id=str(result.id)) == 0
