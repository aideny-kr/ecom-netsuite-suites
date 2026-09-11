import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.services.chat.write_confirmation_service import build_confirmation_payload, mint_confirmation_token
from app.services.transaction_ops import accounting_group as mod
from tests.test_tax_correction import proposal


def group_fixture(count=2):
    session_id = uuid4()
    tenant_id, actor_id = uuid4(), uuid4()
    members = []
    for i in range(count):
        p = proposal()
        p.update(case_id=str(uuid4()), tenant_id=str(tenant_id), record_id=str(20 + i))
        name = f"ext__{p['connector_id'].replace('-', '')}__ns_updateRecord"
        params = {"recordType": "invoice", "recordId": p["record_id"], "data": '{"taxRate":4.9999404}'}
        card = build_confirmation_payload(
            mutation_type="update",
            record_type="invoice",
            tool_name=name,
            tool_input=params,
            session_id=str(session_id),
            current_record=p["before"],
        )
        card.accounting_review = p
        value = {**card.model_dump(mode="json"), "accounting_group_child": True}
        members.append(
            {"case_id": p["case_id"], "order_reference": f"R{i}", "confirmation_id": str(uuid4()), "card": value}
        )
    group = {"group_id": "abc", "members": members, "concurrency": 3}
    params = {"manifest_digest": mod.digest(group), "confirmation_ids": [m["confirmation_id"] for m in members]}
    so = dict(
        type="write_confirmation",
        mutation_type="execute",
        status="pending",
        tool_name=mod.GROUP_TOOL,
        tool_input=params,
        editable_slots=[],
        accounting_group=group,
        confirmation_token=mint_confirmation_token(mod.GROUP_TOOL, params, [], str(session_id)),
    )
    session = SimpleNamespace(id=session_id, tenant_id=tenant_id, user_id=actor_id)
    return so, session


@pytest.mark.parametrize("tamper", ["amount", "add_member", "session", "record", "manifest", "child_signature"])
def test_parent_binds_exact_members_amounts_and_each_signed_card(tamper):
    so, session = group_fixture()
    assert len(mod.validate_manifest(so, str(session.id))) == 2
    if tamper == "amount":
        so["accounting_group"]["members"][0]["card"]["accounting_review"]["expected_after"]["total"] = "999"
    elif tamper == "add_member":
        so["accounting_group"]["members"].append(deepcopy(so["accounting_group"]["members"][0]))
    elif tamper == "record":
        so["accounting_group"]["members"][0]["card"]["tool_input"]["recordId"] = "other"
    elif tamper == "manifest":
        so["tool_input"]["confirmation_ids"].reverse()
    elif tamper == "child_signature":
        so["accounting_group"]["members"][0]["card"]["confirmation_token"] = "forged"
    else:
        session.id = uuid4()
    with pytest.raises(ValueError):
        mod.validate_manifest(so, str(session.id))


def test_even_server_signed_manifest_cannot_contain_overlapping_invoice_writes():
    so, session = group_fixture()
    a, b = so["accounting_group"]["members"]
    b["card"] = deepcopy(a["card"])
    b["case_id"] = a["case_id"]
    so["tool_input"]["manifest_digest"] = mod.digest(so["accounting_group"])
    so["confirmation_token"] = mint_confirmation_token(mod.GROUP_TOOL, so["tool_input"], [], str(session.id))
    with pytest.raises(ValueError, match="Overlapping"):
        mod.validate_manifest(so, str(session.id))


async def test_bounded_workers_overlap_without_exceeding_three_and_stop_unsent_on_unknown():
    active, maximum = 0, 0
    gate = asyncio.Event()
    stop = asyncio.Event()
    started = []

    async def execute(item):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        started.append(item["id"])
        if active == 3:
            gate.set()
        await gate.wait()
        stop.set()  # Simulated unknown write receipt: no further dispatch.
        active -= 1
        return {**item, "status": "unconfirmed"}

    result = await mod.bounded_map([{"id": i} for i in range(10)], execute, stop=stop)
    assert maximum == 3 and started == [0, 1, 2]
    assert [r["id"] for r in result] == list(range(10))
    assert all("Not submitted" in r["reason"] for r in result[3:])


async def test_cancellation_waits_for_all_workers_and_never_leaves_background_writes():
    started = asyncio.Event()
    active = set()

    async def execute(item):
        active.add(item)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active.remove(item)

    task = asyncio.create_task(mod.bounded_map(range(5), execute))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not active


async def test_real_postgres_locks_protect_same_invoice_and_cap_account_across_connections(monkeypatch):
    engine = create_async_engine(settings.DATABASE_URL_DIRECT or settings.DATABASE_URL)
    monkeypatch.setattr(mod, "engine", engine)
    p = proposal()
    p["scope"]["netsuite_account_id"] = str(uuid4())
    try:
        async with mod.accounting_write_slot(p):
            with pytest.raises(ValueError, match="same|this invoice"):
                async with mod.accounting_write_slot(p):
                    pytest.fail("Duplicate invoice lock was granted")
            async with mod.accounting_write_slot({**p, "record_id": "21"}):
                async with mod.accounting_write_slot({**p, "record_id": "22"}):
                    with pytest.raises(ValueError, match="Three corrections"):
                        async with mod.accounting_write_slot({**p, "record_id": "23"}):
                            pytest.fail("Fourth account writer was admitted")
        async with mod.accounting_write_slot(p):
            pass  # Locks released and safe to reuse after completion.
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "outcome", ["verified", "unverified", "missing_verification", "changed", "duplicate", "wrong_tenant"]
)
async def test_group_uses_existing_human_approval_path_and_persists_per_order_results(monkeypatch, outcome):
    so, session = group_fixture(4)
    parent = SimpleNamespace(id=uuid4(), structured_output=so)
    children = {
        m["confirmation_id"]: SimpleNamespace(structured_output=deepcopy(m["card"]))
        for m in so["accounting_group"]["members"]
    }
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=list(children.values()))
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    if outcome == "changed":
        next(iter(children.values())).structured_output["status"] = "approved"
    elif outcome == "wrong_tenant":
        session.tenant_id = uuid4()
    child_sessions = []

    @asynccontextmanager
    async def factory():
        child_db = MagicMock()
        child_db.rollback = AsyncMock()
        child_db.commit = AsyncMock()
        child_db.scalar = AsyncMock(return_value=session)
        child_sessions.append(child_db)
        yield child_db

    approved = []

    async def original_path(**kwargs):
        wc = kwargs["write_confirm"]
        assert wc["action"] == "approve"
        child = children[wc["confirmation_id"]]
        assert child.structured_output["status"] == "pending"
        approved.append(wc["confirmation_id"])
        child.structured_output = {
            **child.structured_output,
            "status": "approved",
            "accounting_verification": {"status": "needs_review" if outcome == "unverified" else "verified"},
        }
        if outcome == "missing_verification":
            child.structured_output.pop("accounting_verification")
        kwargs["db"].scalar.return_value = child
        yield {"type": "done"}

    audit = AsyncMock()
    claim = AsyncMock(return_value=outcome != "duplicate")
    monkeypatch.setattr(mod, "async_session_factory", factory)
    monkeypatch.setattr(mod, "set_tenant_context", AsyncMock())
    monkeypatch.setattr(mod, "log_event", audit)
    monkeypatch.setattr("app.mcp.tools.transaction_ops_tools._authorize", AsyncMock())
    monkeypatch.setattr("app.services.policy_service.get_active_policy", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.chat.orchestrator._cas_claim_write_confirmation", claim)
    monkeypatch.setattr("app.services.chat.orchestrator.run_chat_turn", original_path)
    monkeypatch.setattr("app.services.transaction_ops.accounting_recovery.refresh_group", AsyncMock())
    tenant_id = uuid4() if outcome == "wrong_tenant" else session.tenant_id
    iterator = mod.run_group_confirmation(
        db=db,
        session=session,
        message=parent,
        so=so,
        action="approve",
        user_id=session.user_id,
        tenant_id=tenant_id,
        correlation_id="test",
    )
    if outcome in {"changed", "duplicate", "wrong_tenant"}:
        with pytest.raises(ValueError):
            _ = [v async for v in iterator]
        assert not approved
    else:
        events = [v async for v in iterator]
        assert len({id(d) for d in child_sessions}) == len(approved)
        if outcome == "verified":
            assert len(approved) == 4
        else:
            assert 1 <= len(approved) <= mod.CONCURRENCY
            assert list(children.values())[-1].structured_output["status"] == "pending"
        assert parent.structured_output["status"] == ("approved" if outcome == "verified" else "indeterminate")
        assert events[-1]["message"]["structured_output"] == parent.structured_output
        per_order = [c.kwargs for c in audit.await_args_list if c.kwargs["action"] == "accounting_group.case.completed"]
        assert len(per_order) == len(approved)
        assert all(c["payload"]["approved_by"] == str(session.user_id) for c in per_order)


@pytest.mark.parametrize("same_invoice", [False, True])
def test_credit_group_targets_invoice_identity_instead_of_unallocated_credit_id(same_invoice):
    from tests.test_accounting_approval_flow import inputs, kind_proposal

    so, session = group_fixture()
    for index, member in enumerate(so["accounting_group"]["members"]):
        p = kind_proposal("credit")
        invoice_id = str(20 if same_invoice else 20 + index)
        p.update(tenant_id=str(session.tenant_id), case_id=member["case_id"], record_id=invoice_id)
        p["proposed_fields"]["apply"]["items"][0]["doc"]["id"] = invoice_id
        p["proposed_fields"]["externalId"] += str(index)
        name, params = inputs(p)
        card = build_confirmation_payload(
            mutation_type="create",
            record_type="creditmemo",
            tool_name=name,
            tool_input=params,
            session_id=str(session.id),
            current_record=None,
        )
        card.accounting_review = p
        member["card"] = {**card.model_dump(mode="json"), "accounting_group_child": True}
        assert member["card"]["record_id"] is None
    so["tool_input"]["manifest_digest"] = mod.digest(so["accounting_group"])
    so["confirmation_token"] = mint_confirmation_token(mod.GROUP_TOOL, so["tool_input"], [], str(session.id))
    if same_invoice:
        with pytest.raises(ValueError, match="Overlapping"):
            mod.validate_manifest(so, str(session.id))
    else:
        assert len(mod.validate_manifest(so, str(session.id))) == 2
