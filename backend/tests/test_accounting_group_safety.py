"""Regression checks for group approval authorization, cancellation audit and rejection."""

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from app.models.chat import ChatMessage
from app.services.chat import external_tool_audit, orchestrator, tools
from app.services.chat.write_confirmation_service import WriteConfirmationPayload
from app.services.transaction_ops import accounting_group as group
from tests.test_accounting_group import group_fixture
from tests.test_write_confirm_orchestrator import _make_db


@pytest.fixture(autouse=True)
def isolated_authorization_database(monkeypatch):
    @asynccontextmanager
    async def factory():
        yield AsyncMock()

    monkeypatch.setattr(group, "_authorization_session_factory", factory)


def child_object(so, session):
    member = so["accounting_group"]["members"][0]
    return ChatMessage(
        id=UUID(member["confirmation_id"]),
        tenant_id=session.tenant_id,
        session_id=session.id,
        role="assistant",
        content="",
        structured_output=deepcopy(member["card"]),
        created_at=datetime.now(timezone.utc),
    )


@asynccontextmanager
async def no_lock(_):
    yield


@pytest.mark.asyncio
async def test_direct_group_child_cannot_bypass_parent_authorization(monkeypatch):
    so, session = group_fixture(1)
    child = child_object(so, session)
    db = _make_db(child)
    db.info = {}
    auth = AsyncMock(side_effect=ValueError("permission_denied"))
    policy = AsyncMock(return_value=SimpleNamespace(tool_allowlist=["some_other_tool"]))
    execute = AsyncMock(return_value=json.dumps({"success": True}))
    monkeypatch.setattr("app.mcp.tools.transaction_ops_tools._authorize", auth)
    monkeypatch.setattr("app.services.policy_service.get_active_policy", policy)
    monkeypatch.setattr(group, "accounting_write_slot", no_lock)
    monkeypatch.setattr("app.services.transaction_ops.tax_correction.validate_approved", AsyncMock())
    monkeypatch.setattr(
        "app.services.transaction_ops.tax_correction.verify_after", AsyncMock(return_value={"status": "verified"})
    )
    monkeypatch.setattr(orchestrator, "execute_tool_call", execute)
    monkeypatch.setattr(orchestrator, "log_event", AsyncMock())
    monkeypatch.setattr("app.services.mcp_connector_service.get_mcp_connector", AsyncMock(return_value=None))
    _ = [
        e
        async for e in orchestrator.run_chat_turn(
            db=db,
            session=session,
            user_message="",
            user_id=session.user_id,
            tenant_id=session.tenant_id,
            write_confirm={"action": "approve", "confirmation_id": str(child.id)},
        )
    ]
    execute.assert_not_awaited()
    assert child.structured_output["status"] == "pending"
    assert any("belongs to a group" in e.get("error", "") for e in _)
    auth.assert_not_awaited()
    policy.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_group_call_keeps_durable_exact_approval_to_call_link(monkeypatch):
    so, session = group_fixture(1)
    child = child_object(so, session)
    parent = SimpleNamespace(id=uuid4(), structured_output=so)
    db = MagicMock()
    db.scalar = AsyncMock(return_value=child)
    db.commit = AsyncMock()
    child_db = _make_db(child)
    child_db.info = {}
    child_db.scalar = AsyncMock(return_value=session)
    child_db.rollback = AsyncMock()
    child_db.expire_all = MagicMock()

    @asynccontextmanager
    async def factory():
        yield child_db

    async def claim(db, msg, card, status):
        msg.structured_output = {**card, "status": status}
        return True

    started = asyncio.Event()

    async def native(*args, **kwargs):
        started.set()
        await asyncio.Future()

    audit = AsyncMock()
    tool_audit = AsyncMock()
    interrupted = AsyncMock()
    monkeypatch.setattr(group, "async_session_factory", factory)
    monkeypatch.setattr(group, "set_tenant_context", AsyncMock())
    monkeypatch.setattr(group, "accounting_write_slot", no_lock)
    monkeypatch.setattr(group, "log_event", audit)
    monkeypatch.setattr(group, "persist_interrupted_group", interrupted)
    monkeypatch.setattr("app.mcp.tools.transaction_ops_tools._authorize", AsyncMock())
    monkeypatch.setattr("app.services.policy_service.get_active_policy", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.transaction_ops.tax_correction.validate_approved", AsyncMock())
    monkeypatch.setattr(orchestrator, "_cas_claim_write_confirmation", claim)
    monkeypatch.setattr(orchestrator, "execute_tool_call", tools.execute_tool_call)
    monkeypatch.setattr(tools, "_execute_external_tool", native)
    monkeypatch.setattr(orchestrator, "log_event", audit)
    monkeypatch.setattr(external_tool_audit, "append_event", tool_audit)

    async def run():
        return [
            e
            async for e in group.run_group_confirmation(
                db=db,
                session=session,
                message=parent,
                so=so,
                action="approve",
                user_id=session.user_id,
                tenant_id=session.tenant_id,
                correlation_id="parent-review-correlation",
            )
        ]

    task = asyncio.create_task(run())
    await asyncio.wait_for(started.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.structured_output["status"] == "executing"
    assert [c.kwargs["action"] for c in tool_audit.await_args_list] == ["tool.requested", "tool.interrupted"]
    assert [c.kwargs["action"] for c in audit.await_args_list] == ["accounting_group.approve.requested"]
    requested = tool_audit.await_args_list[0].kwargs
    assert requested["correlation_id"] != "parent-review-correlation"
    assert requested["payload"]["approval"] == {
        "confirmation_id": str(child.id),
        "group_approval_id": str(parent.id),
        "manifest_digest": so["tool_input"]["manifest_digest"],
    }
    assert tool_audit.await_args_list[1].kwargs["payload"]["approval"] == requested["payload"]["approval"]
    interrupted.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_reject_reports_incomplete_when_child_remains_pending(monkeypatch):
    so, session = group_fixture(1)
    child = child_object(so, session)
    parent = SimpleNamespace(id=uuid4(), structured_output=so)
    db = MagicMock()
    db.scalar = AsyncMock(return_value=child)
    db.commit = AsyncMock()
    child_db = MagicMock()
    child_db.scalar = AsyncMock(side_effect=[session, child])
    child_db.rollback = AsyncMock()
    child_db.commit = AsyncMock()

    @asynccontextmanager
    async def factory():
        yield child_db

    # A permission removal between parent validation and child processing blocks child rejection.
    auth = AsyncMock(side_effect=[None, ValueError("permission_denied")])
    monkeypatch.setattr(group, "async_session_factory", factory)
    monkeypatch.setattr(group, "set_tenant_context", AsyncMock())
    monkeypatch.setattr(group, "log_event", AsyncMock())
    monkeypatch.setattr("app.mcp.tools.transaction_ops_tools._authorize", auth)
    monkeypatch.setattr(orchestrator, "_cas_claim_write_confirmation", AsyncMock(return_value=True))
    events = [
        e
        async for e in group.run_group_confirmation(
            db=db,
            session=session,
            message=parent,
            so=so,
            action="reject",
            user_id=session.user_id,
            tenant_id=session.tenant_id,
            correlation_id="review",
        )
    ]
    assert events[-1]["message"]["content"].startswith("Rejected 0 of 1 proposed corrections.")
    assert "incomplete" in events[-1]["message"]["content"]
    assert parent.structured_output["status"] == "indeterminate"
    assert parent.structured_output["accounting_group"]["members"][0]["card"]["status"] == "pending"


@pytest.mark.parametrize("blocked_by", ["permission", "policy", "revoked_during_preflight"])
async def test_single_accounting_approval_rechecks_current_permission_and_policy(monkeypatch, blocked_by):
    so, session = group_fixture(1)
    child = child_object(so, session)
    child.structured_output.pop("accounting_group_child")
    db = _make_db(child)
    db.info = {}
    auth = AsyncMock(side_effect=ValueError("permission_denied")) if blocked_by == "permission" else AsyncMock()
    policy = AsyncMock(return_value=None)
    decision = MagicMock(return_value={"allowed": blocked_by != "policy"})
    execute = AsyncMock()

    async def preflight(*args):
        if blocked_by == "revoked_during_preflight":
            auth.side_effect = ValueError("permission_denied")

    monkeypatch.setattr("app.mcp.tools.transaction_ops_tools._authorize", auth)
    monkeypatch.setattr("app.services.policy_service.get_active_policy", policy)
    monkeypatch.setattr("app.services.policy_service.evaluate_tool_call", decision)
    monkeypatch.setattr(group, "accounting_write_slot", no_lock)
    monkeypatch.setattr("app.services.transaction_ops.tax_correction.validate_approved", preflight)
    monkeypatch.setattr(orchestrator, "execute_tool_call", execute)
    monkeypatch.setattr(orchestrator, "log_event", AsyncMock())
    events = [
        e
        async for e in orchestrator.run_chat_turn(
            db=db,
            session=session,
            user_message="",
            user_id=session.user_id,
            tenant_id=session.tenant_id,
            write_confirm={"action": "approve", "confirmation_id": str(child.id)},
        )
    ]
    execute.assert_not_awaited()
    assert any(e.get("type") == "error" for e in events)
    assert auth.await_count == (2 if blocked_by == "revoked_during_preflight" else 1)


@pytest.mark.parametrize("tamper", ["confirmation_id", "session_id", "tenant_id", "action", "card_digest"])
def test_group_child_requires_exact_server_execution_context(tamper):
    so, session = group_fixture(1)
    member = so["accounting_group"]["members"][0]
    context = dict(
        confirmation_id=member["confirmation_id"],
        session_id=str(session.id),
        tenant_id=str(session.tenant_id),
        action="approve",
        card_digest=group.digest(member["card"]),
        group_approval_id=str(uuid4()),
        manifest_digest=so["tool_input"]["manifest_digest"],
    )
    db = SimpleNamespace(info={"accounting_group_execution": context})
    assert group.group_child_context(
        db, member["card"], member["confirmation_id"], session.id, session.tenant_id, "approve"
    )["group_approval_id"]
    context[tamper] = "changed"
    with pytest.raises(ValueError, match="belongs to a group"):
        group.group_child_context(
            db, member["card"], member["confirmation_id"], session.id, session.tenant_id, "approve"
        )


@pytest.mark.parametrize("interrupt", [False, True])
async def test_preparation_publishes_children_only_with_complete_parent(monkeypatch, interrupt):
    so, session = group_fixture(7)
    members = so["accounting_group"]["members"]
    selection = {
        "group_id": "test",
        "scope": {},
        "members": [{"case_id": m["case_id"], "order_reference": m["order_reference"]} for m in members],
    }
    db = AsyncMock()
    db.add = MagicMock()
    db.info = {"accounting_group_selection": selection}
    completed = asyncio.Event()
    prepared_count = 0
    children = []

    @asynccontextmanager
    async def factory():
        child_db = AsyncMock()
        child_db.add = MagicMock()
        children.append(child_db)
        yield child_db

    async def evidence(*args, **kwargs):
        if interrupt and prepared_count >= 3:
            completed.set()
            await asyncio.Event().wait()
        return {"success": True}

    async def candidate(**kwargs):
        nonlocal prepared_count
        prepared_count += 1
        member = next(m for m in members if m["case_id"] == kwargs["case_id"])
        return WriteConfirmationPayload(**member["card"]), "test"

    interrupted = AsyncMock()
    monkeypatch.setattr(group, "async_session_factory", factory)
    monkeypatch.setattr(group, "set_tenant_context", AsyncMock())
    monkeypatch.setattr(group, "log_event", AsyncMock())
    monkeypatch.setattr(group, "record_preparation_interrupted", interrupted)
    monkeypatch.setattr("app.mcp.tools.transaction_ops_tools.execute_accounting_evidence", evidence)
    monkeypatch.setattr("app.services.transaction_ops.tax_correction.candidate_confirmation", candidate)
    task = asyncio.create_task(
        group.prepare_group_confirmation(
            db=db,
            tenant_id=session.tenant_id,
            actor_id=session.user_id,
            session_id=str(session.id),
            correlation_id="test",
            tools=[],
            policy=None,
        )
    )
    if interrupt:
        await asyncio.wait_for(completed.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        interrupted.assert_awaited_once()
        db.add.assert_not_called()
    else:
        card, _ = await task
        assert len(card.accounting_group["members"]) == 7
        db.add.assert_not_called()  # A failed model handoff can still commit a plain error safely.
        parent = ChatMessage(
            tenant_id=session.tenant_id,
            session_id=session.id,
            role="assistant",
            content="Review",
            structured_output=card.model_dump(mode="json"),
        )
        group.stage_group_children(db, parent)
        assert db.add.call_count == 7
        assert {str(c.args[0].id) for c in db.add.call_args_list} == set(card.tool_input["confirmation_ids"])
        db.commit.assert_not_awaited()  # Orchestrator atomically commits children with the parent.
    for child_db in children:
        child_db.add.assert_not_called()
    assert sum(child.commit.await_count for child in children) == (3 if interrupt else 7)


async def test_final_authorization_ignores_identity_mapped_policy_after_revocation(monkeypatch):
    from sqlalchemy import create_engine, insert, update
    from sqlalchemy.orm import Session

    from app.models.policy_profile import PolicyProfile
    from app.services.policy_service import get_active_policy

    engine = create_engine("sqlite:///:memory:")
    PolicyProfile.__table__.create(engine)
    tenant_id, actor_id, policy_id = uuid4(), uuid4(), uuid4()
    tool_name = "ext__test__ns_updateRecord"
    with engine.begin() as connection:
        connection.execute(
            insert(PolicyProfile).values(
                id=policy_id,
                tenant_id=tenant_id,
                name="test",
                version=1,
                tool_allowlist=[tool_name],
            )
        )

    class Wrapper:
        def __init__(self, session):
            self.session = session

        async def execute(self, query):
            return self.session.execute(query)

    @asynccontextmanager
    async def fresh_factory():
        with Session(engine, expire_on_commit=False) as fresh:
            yield Wrapper(fresh)

    authorize = AsyncMock()
    monkeypatch.setattr(group, "_authorization_session_factory", fresh_factory)
    monkeypatch.setattr(group, "set_tenant_context", AsyncMock())
    monkeypatch.setattr("app.mcp.tools.transaction_ops_tools._authorize", authorize)
    try:
        with Session(engine, expire_on_commit=False) as retained:
            db = Wrapper(retained)
            old_policy = await get_active_policy(db, tenant_id)
            retained.commit()
            await group.authorize_accounting_write(db, tenant_id, actor_id, tool_name, {})
            with engine.begin() as connection:
                connection.execute(
                    update(PolicyProfile).where(PolicyProfile.id == policy_id).values(tool_allowlist=["other"])
                )
            assert old_policy.tool_allowlist == [tool_name]  # Retained worker's identity map is stale.
            with pytest.raises(ValueError, match="Current policy blocks"):
                await group.authorize_accounting_write(db, tenant_id, actor_id, tool_name, {})
            assert all(call.kwargs["fresh"] is True for call in authorize.await_args_list)
            assert all(call.args[0]["db"] is not db for call in authorize.await_args_list)
    finally:
        engine.dispose()


async def test_parent_and_children_rollback_together_in_postgres(db):
    from sqlalchemy import select

    from app.models.chat import ChatSession

    so, session = group_fixture(2)
    await group.set_tenant_context(db, str(session.tenant_id))
    db.add(ChatSession(id=session.id, tenant_id=session.tenant_id, user_id=session.user_id, title="Atomic review test"))
    await db.flush()
    parent = ChatMessage(
        id=uuid4(),
        tenant_id=session.tenant_id,
        session_id=session.id,
        role="assistant",
        content="Review",
        structured_output=so,
    )
    ids = [parent.id, *(UUID(i) for i in so["tool_input"]["confirmation_ids"])]
    transaction = await db.begin_nested()
    db.add(parent)
    group.stage_group_children(db, parent)
    await db.flush()
    query = select(ChatMessage.id).where(ChatMessage.id.in_(ids), ChatMessage.tenant_id == session.tenant_id)
    assert len((await db.scalars(query)).all()) == 3
    await transaction.rollback()  # A later handoff/persistence failure cannot retain hidden children.
    assert not (await db.scalars(query)).all()
