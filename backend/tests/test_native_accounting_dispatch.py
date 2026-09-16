from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.tools.transaction_ops_tools import _ToolError
from app.models.audit import AuditEvent
from app.services.chat.orchestrator import _cas_claim_write_confirmation
from app.services.chat.tools import execute_tool_call
from app.services.transaction_ops import accounting_recovery as recovery
from app.services.transaction_ops import native_accounting_dispatch as dispatch
from app.services.transaction_ops import native_accounting_service as service
from app.services.transaction_ops import native_accounting_transport as transport
from app.services.transaction_ops.native_accounting_protocol import profile_binding
from app.services.transaction_ops.resolution_plan import fingerprint
from tests.test_accounting_recheck import approved_credit  # noqa: F401
from tests.test_native_accounting_service import prepared


@pytest.fixture
async def native_claim(db, approved_credit, monkeypatch):  # noqa: F811
    from tests.conftest import enable_feature_flag

    actor, config, case, message, _, _ = approved_credit
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
    p, _ = prepared()
    p.update(
        tenant_id=str(actor.tenant_id),
        case_id=str(case.id),
        config_id=str(config.id),
        connection_id=str(config.netsuite_connection_id),
        scope=case.scope_json,
    )
    account = p["scope"]["netsuite_account_id"]
    p["native_profile"]["account_id"] = account
    p["native_profile"]["revision"] = fingerprint({k: v for k, v in p["native_profile"].items() if k != "revision"})
    p["native_request"]["accountId"] = account
    p["native_preview"]["accountId"] = account
    p["native_preview"]["profile"] = profile_binding(p["native_profile"])
    card, _ = await service.confirmation(db, actor.tenant_id, actor.id, str(message.session_id), p, None, None)
    context = {"confirmation_id": str(message.id)}
    so = recovery.execution_claim(
        card.model_dump(mode="json"), message.id, actor.id, context, now=datetime.now(timezone.utc)
    )
    message.structured_output = so
    await db.flush()
    assert await _cas_claim_write_confirmation(db, message, so, "executing")
    await db.refresh(message)

    @asynccontextmanager
    async def factory():
        async with AsyncSession(
            bind=await db.connection(), expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as auth_db:
            yield auth_db

    db.info["accounting_authorization_session_factory"] = factory
    # External source/native oracle only. Real token binding, permissions, claim,
    # reservation, DB audit and dispatch choke point remain exercised.
    preflight = AsyncMock()
    monkeypatch.setattr(service, "validate_approved", preflight)
    return actor, message, context, preflight


@pytest.mark.parametrize("outcome", ["confirmed", "lost_response", "process_stopped"])
async def test_reservation_precedes_send_and_duplicate_or_crash_never_resends(db, native_claim, monkeypatch, outcome):
    actor, message, context, preflight = native_claim
    so = message.structured_output

    class Stopped(BaseException):
        pass

    calls = []

    async def native_send(*args, **kwargs):
        audit = await db.scalar(
            select(AuditEvent).where(AuditEvent.action == dispatch.RESERVED, AuditEvent.resource_id == str(message.id))
        )
        assert audit and audit.actor_id == actor.id
        assert audit.payload["approval_context"] == context
        assert audit.payload["financial_writes"] == 0
        assert args[4] == "apply" and args[5]["approval_audit_id"] == str(audit.id)
        calls.append(args[5])
        if outcome == "lost_response":
            raise TimeoutError("Response lost after native save")
        if outcome == "process_stopped":
            raise Stopped()
        return {
            "success": True,
            "schema_version": 1,
            "status": "posted_pending_independent_verification",
            "record_type": "creditmemo",
            "record_id": so["accounting_review"]["record_id"],
            "work_key": so["accounting_execution"]["operation_key"],
            "financial_writes": 1,
        }

    monkeypatch.setattr(transport, "_request", native_send)

    async def run():
        return await dispatch.execute(db, actor.tenant_id, actor.id, str(message.session_id), so["tool_input"], context)

    if outcome == "process_stopped":
        with pytest.raises(Stopped):
            await run()
        await db.rollback()
        await db.refresh(actor)
    else:
        result = await run()
        assert result["outcome_indeterminate"] is (outcome == "lost_response")
    await db.refresh(message)
    result = await run()
    assert result["outcome_indeterminate"] is True and result["retry_allowed"] is False
    assert len(calls) == 1
    events = list(
        await db.scalars(
            select(AuditEvent).where(AuditEvent.action == dispatch.RESERVED, AuditEvent.resource_id == str(message.id))
        )
    )
    assert len(events) == 1


@pytest.mark.parametrize(
    "tamper", ["tenant", "actor", "context", "signature", "proposal", "params", "claim", "audit", "revoked"]
)
async def test_bad_approval_cannot_reach_native_transport(db, native_claim, monkeypatch, tamper):
    actor, message, context, preflight = native_claim
    so = deepcopy(message.structured_output)
    params = so["tool_input"]
    tenant = actor.tenant_id
    actor_id = actor.id
    if tamper == "tenant":
        tenant = uuid4()
    elif tamper == "actor":
        actor_id = uuid4()
    elif tamper == "context":
        context = {**context, "group_approval_id": str(uuid4())}
    elif tamper == "signature":
        so["confirmation_token"] = "fake"
    elif tamper == "proposal":
        so["accounting_review"]["source"]["total"] = "1"
    elif tamper == "params":
        params = {**params, "recordId": "999"}
    elif tamper == "claim":
        so["accounting_execution"]["operation_key"] = "changed"
    elif tamper == "audit":
        so["accounting_execution"]["accepted_at"] = "2000-01-01T00:00:00+00:00"
    elif tamper == "revoked":
        actor.is_active = False
    message.structured_output = so
    await db.flush()
    send = AsyncMock()
    monkeypatch.setattr(transport, "_request", send)
    with pytest.raises((ValueError, KeyError, _ToolError)):
        await dispatch.execute(db, tenant, actor_id, str(message.session_id), params, context)
    send.assert_not_awaited()
    preflight.assert_not_awaited()


async def test_unapproved_generic_tool_call_never_reaches_native_dispatch(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(dispatch, "execute", send)
    result = await execute_tool_call(
        tool_name=service.TOOL, tool_input={}, tenant_id=uuid4(), actor_id=uuid4(), db=None, correlation_id=None
    )
    assert "hitl_required" in result
    send.assert_not_awaited()
