"""HITL audit-gap regression: the chat-agent MCP tool ``recon.approve_match`` must
write exactly one ``recon.approve`` audit event per line it approves — mirroring the
REST single-approve endpoint. The guard paths (not-found / already-approved / locked)
must NOT write an audit row.
"""

import uuid

from sqlalchemy import func, select

from app.mcp.tools.recon_approve import execute
from app.models.audit import AuditEvent
from tests.conftest import create_test_recon_result, create_test_recon_run


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


async def test_approve_match_writes_exactly_one_audit_event(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id)
    result = await create_test_recon_result(db, tenant_a.id, run.id, status="suggested")
    await db.flush()

    user_id = uuid.uuid4()
    out = await execute(
        {"result_id": str(result.id)},
        db=db,
        tenant_id=tenant_a.id,
        user_id=user_id,
    )

    assert out["success"] is True
    assert out["status"] == "approved"

    rows = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "recon.approve",
                    AuditEvent.resource_id == str(result.id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    event = rows[0]
    assert event.category == "reconciliation"
    assert event.action == "recon.approve"
    assert event.resource_type == "reconciliation_result"
    assert event.resource_id == str(result.id)
    assert event.tenant_id == tenant_a.id
    assert event.actor_id == user_id


async def test_already_approved_writes_no_audit_event(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id)
    result = await create_test_recon_result(db, tenant_a.id, run.id, status="approved")
    await db.flush()

    out = await execute(
        {"result_id": str(result.id)},
        db=db,
        tenant_id=tenant_a.id,
        user_id=uuid.uuid4(),
    )

    assert out["success"] is False
    assert await _count_audit(db, resource_id=str(result.id)) == 0


async def test_malformed_result_id_returns_structured_error(db, tenant_a):
    """R4-B #14 sibling: the same ``uuid.UUID(<llm-param>)`` one-liner as
    recon.get_exceptions' run_id — a malformed id must return the structured
    ``{"success": False, "error": ...}`` shape (never an uncaught ValueError
    through the dispatch boundary) and write no audit row."""
    out = await execute(
        {"result_id": "not-a-uuid"},
        db=db,
        tenant_id=tenant_a.id,
        user_id=uuid.uuid4(),
    )

    assert out["success"] is False
    assert "result_id" in out["error"]
    assert await _count_audit(db, resource_id="not-a-uuid") == 0


async def test_locked_writes_no_audit_event(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id)
    result = await create_test_recon_result(db, tenant_a.id, run.id, status="locked")
    await db.flush()

    out = await execute(
        {"result_id": str(result.id)},
        db=db,
        tenant_id=tenant_a.id,
        user_id=uuid.uuid4(),
    )

    assert out["success"] is False
    assert await _count_audit(db, resource_id=str(result.id)) == 0
