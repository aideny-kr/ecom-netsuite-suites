from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.chat import ChatMessage
from app.services.audit_service import log_event
from app.services.transaction_ops import accounting_completion as mod
from tests.conftest import enable_feature_flag
from tests.test_accounting_history import reconciled_credit  # noqa: F401
from tests.test_accounting_recheck import approved_credit  # noqa: F401


@pytest.fixture
async def ready(db, reconciled_credit, monkeypatch):  # noqa: F811
    actor, config, case, message, run, finding = reconciled_credit
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
    claim = message.structured_output["accounting_execution"]
    await log_event(
        db,
        actor.tenant_id,
        "transaction_ops",
        "accounting_correction.approval_claimed",
        actor_id=actor.id,
        resource_type="chat_message",
        resource_id=str(message.id),
        payload={"evidence_digest": claim["evidence_digest"], "accepted_at": claim["accepted_at"]},
    )
    await db.commit()

    @asynccontextmanager
    async def lock(_p, **_kwargs):
        yield

    monkeypatch.setattr("app.services.transaction_ops.accounting_group.accounting_write_slot", lock)
    return actor, config, case, message, run, finding


async def test_finished_recheck_publishes_once_with_original_approver_and_record_links(db, ready, monkeypatch):
    actor, _, case, message, run, _ = ready
    prepare = AsyncMock(side_effect=AssertionError("Matched order must not prepare another write"))
    monkeypatch.setattr(mod, "prepare_next", prepare)
    assert message.id in await mod.candidates(db, actor.tenant_id, datetime.now(timezone.utc), limit=10)
    published_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    result = await mod.complete(db, actor.tenant_id, message.id, now=published_at)
    assert result["status"] == "reconciled"
    assert result["financial_writes"] == 0
    await db.refresh(message)
    receipt = message.structured_output["accounting_receipt"]
    assert receipt["approved_by"]["id"] == str(actor.id)
    assert receipt["checked_at"] == run.progress_json["settlement"]["checked_at"]
    assert receipt["published_at"] == published_at.isoformat()
    assert receipt["checked_at"] != receipt["published_at"]
    assert receipt["reconciliation_run_id"] == str(run.id)
    assert {link["record_type"] for link in receipt["record_links"]} >= {"salesorder", "creditmemo"}
    assert receipt["balance"]["amounts"]["order_total"]["delta"] == "0.00"
    assert receipt["cash_settlement"] == "not_verified"
    again = await mod.complete(db, actor.tenant_id, message.id)
    assert again["status"] == "not_due"
    messages = list(await db.scalars(select(ChatMessage).where(ChatMessage.session_id == message.session_id)))
    assert len(messages) == 2
    published = next(m for m in messages if m.id != message.id)
    assert "/audit)" in published.content
    assert "Reconciled" in published.content
    assert published.input_tokens == published.output_tokens == 0
    events = list(await db.scalars(select(AuditEvent).where(AuditEvent.action == "accounting_plan.completion")))
    assert len(events) == 1 and events[0].payload["approved_by"]["id"] == str(actor.id)
    prepare.assert_not_awaited()


@pytest.mark.parametrize("variant", ["wrong_run", "missing_audit", "disabled_actor", "wrong_tenant"])
async def test_invalid_authority_or_scope_never_publishes_success(db, ready, tenant_b, variant, monkeypatch):
    actor, _, _, message, run, finding = ready
    if variant == "wrong_run":
        from uuid import uuid4

        so = deepcopy(message.structured_output)
        so["accounting_completion"]["run_id"] = str(uuid4())
        message.structured_output = so
    elif variant == "missing_audit":
        # Corrupt the claimed acceptance time without forging an append-only audit.
        so = deepcopy(message.structured_output)
        so["accounting_execution"]["accepted_at"] = datetime.now(timezone.utc).isoformat()
        message.structured_output = so
    elif variant == "disabled_actor":
        actor.is_active = False
    await db.commit()
    prepare = AsyncMock(side_effect=AssertionError("Invalid provenance cannot prepare"))
    monkeypatch.setattr(mod, "prepare_next", prepare)
    result = await mod.complete(db, tenant_b.id if variant == "wrong_tenant" else actor.tenant_id, message.id)
    assert result["status"] in {"pending", "not_due"}
    await db.refresh(message)
    assert not message.structured_output.get("accounting_receipt")
    prepare.assert_not_awaited()


async def test_transient_failure_is_bounded_and_never_replays_financial_operation(db, ready, monkeypatch):
    actor, _, _, message, _, _ = ready
    tenant_id, message_id, actor_id = actor.tenant_id, message.id, actor.id
    evidence = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(mod, "_evidence", evidence)
    now = datetime.now(timezone.utc)
    for index in range(3):
        result = await mod.complete(db, tenant_id, message_id, now=now + timedelta(minutes=4 * index))
        assert result["status"] == ("blocked" if index == 2 else "pending")
    assert (await mod.complete(db, tenant_id, message_id, now=now + timedelta(hours=1)))["status"] == "not_due"
    assert evidence.await_count == 3
    await db.refresh(message)
    assert message.structured_output["status"] == "approved"
    assert message.structured_output["accounting_execution"]["approved_by"] == str(actor_id)


async def test_reopened_case_never_gets_full_success_from_historical_match(db, ready, monkeypatch):
    actor, _, case, message, _, _ = ready
    case.status = "open"
    await db.commit()
    prepare = AsyncMock(side_effect=AssertionError("Historical match is not a fresh difference"))
    monkeypatch.setattr(mod, "prepare_next", prepare)
    assert (await mod.complete(db, actor.tenant_id, message.id))["status"] == "partially_resolved"
    assert message.structured_output["accounting_receipt"]["next_step"]["status"] == "blocked"
    prepare.assert_not_awaited()


async def test_interrupted_last_read_attempt_finishes_as_review_instead_of_disappearing(db, ready, monkeypatch):
    actor, _, _, message, _, _ = ready
    so = deepcopy(message.structured_output)
    so["accounting_completion"].update(status="running", attempts=3)
    message.structured_output = so
    await db.commit()
    reader = AsyncMock(side_effect=AssertionError("Exhausted work must not start more reads"))
    monkeypatch.setattr(mod, "_evidence", reader)
    assert (await mod.complete(db, actor.tenant_id, message.id))["status"] == "blocked"
    reader.assert_not_awaited()
    assert message.structured_output["accounting_completion"]["status"] == "blocked"


async def test_preparation_uses_fresh_evidence_and_keeps_new_correction_pending(db, ready, monkeypatch):
    from app.services.chat.write_confirmation_service import build_confirmation_payload
    from app.services.transaction_ops.resolution_plan import proposal_plan
    from tests.test_accounting_approval_flow import inputs, kind_proposal

    actor, _, _, message, _, _ = ready
    old = message.structured_output["accounting_review"]
    p = kind_proposal("sales_order")
    p.update({key: old[key] for key in ("tenant_id", "case_id", "scope", "order_reference")})
    p["resolution_plan"] = proposal_plan(p, {})
    tool, params = inputs(p)
    card = build_confirmation_payload(
        mutation_type="update",
        record_type="salesorder",
        tool_name=tool,
        tool_input=params,
        session_id=str(message.session_id),
        current_record=p["before"],
    )
    card.accounting_review = p

    async def fresh(_params, context):
        assert "accounting_correction_candidate" not in context["db"].info
        context["db"].info["accounting_correction_candidate"] = p
        return {"success": True, "accounting_evidence": {}}

    monkeypatch.setattr("app.mcp.tools.transaction_ops_tools.execute_accounting_evidence", fresh)
    monkeypatch.setattr("app.services.chat.tools.build_all_tool_definitions", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.policy_service.get_active_policy", AsyncMock(return_value=None))
    confirmation = AsyncMock(return_value=(card, "Review sales order alignment"))
    monkeypatch.setattr("app.services.transaction_ops.tax_correction.candidate_confirmation", confirmation)
    db.info["accounting_correction_candidate"] = old
    child, result = await mod.prepare_next(db, actor.tenant_id, message, actor.id)
    assert child.structured_output["status"] == "pending"
    assert result["status"] == "awaiting_approval"
    assert child.structured_output["accounting_plan_predecessor"] == str(message.id)
    assert "accounting_execution" not in child.structured_output
    db.add(child)
    await db.flush()
    duplicate, reason = await mod.prepare_next(db, actor.tenant_id, message, actor.id)
    assert duplicate is None and reason["existing_confirmation_id"] == str(child.id)
    confirmation.assert_awaited_once()


async def test_real_remaining_difference_reports_verified_result_even_if_next_preparation_fails(db, ready, monkeypatch):
    from uuid import uuid4

    from app.services.transaction_ops import accounting_recheck
    from app.services.transaction_ops.accounting_recovery import execution_claim
    from app.services.transaction_ops.runner import run_investigation
    from tests.test_transaction_balance_report import evidence

    actor, _, case, previous, _, _ = ready
    so = deepcopy(previous.structured_output)
    so.pop("accounting_completion", None)
    message_id = uuid4()
    so = execution_claim(so, message_id, actor.id, {}, now=datetime.now(timezone.utc))
    message = ChatMessage(
        id=message_id,
        tenant_id=actor.tenant_id,
        session_id=previous.session_id,
        role="assistant",
        content="",
        structured_output=so,
    )
    db.add(message)
    await db.flush()
    claim = so["accounting_execution"]
    await log_event(
        db,
        actor.tenant_id,
        "transaction_ops",
        "accounting_correction.approval_claimed",
        actor_id=actor.id,
        resource_type="chat_message",
        resource_id=str(message.id),
        payload={"evidence_digest": claim["evidence_digest"], "accepted_at": claim["accepted_at"]},
    )
    run = await accounting_recheck.queue(db, actor.tenant_id, message, actor.id, now=datetime.now(timezone.utc))
    await db.commit()
    source, target, _, _, _ = evidence()
    observed = datetime.now(timezone.utc)
    source["read_at"] = target["observed_at"] = observed.isoformat()
    target["orders"][0]["header"].update(total="121.00", taxTotal="20.00")
    refund = {"complete": True, "amount": "0.00", "currency": "USD", "order_reference": case.order_reference}
    guard = AsyncMock(side_effect=AssertionError("Completion must not post a financial change"))
    await run_investigation(
        db,
        actor.tenant_id,
        run.id,
        _clock=lambda: observed,
        _enabled=AsyncMock(return_value=True),
        _source_reader=AsyncMock(return_value=source),
        _target_reader=AsyncMock(return_value=target),
        _source_refunds_reader=AsyncMock(return_value=refund),
        _target_refunds_reader=AsyncMock(return_value=refund),
        _guard_reader=guard,
        _celigo_reader=guard,
        _create_reader=guard,
    )
    await db.refresh(run)
    assert run.progress_json["settlement"]["status"] == "difference"
    preparation = AsyncMock(side_effect=ValueError("metadata unavailable"))
    monkeypatch.setattr(mod, "prepare_next", preparation)
    result = await mod.complete(db, actor.tenant_id, message.id)
    assert result["status"] == "partially_resolved"
    preparation.assert_awaited_once()
    await db.refresh(message)
    assert message.structured_output["accounting_receipt"]["next_step"]["status"] == "blocked"
    assert message.structured_output["accounting_receipt"]["balance"]["amounts"]["order_total"]["delta"] == "-1.00"
    guard.assert_not_awaited()
