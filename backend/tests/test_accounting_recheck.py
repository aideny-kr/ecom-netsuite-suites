"""Actual DB/runner proof for the read-only step after a verified chat credit."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.chat import ChatMessage, ChatSession
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import accounting_recheck, case_service
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.runner import build_report, run_investigation
from tests.test_accounting_approval_flow import kind_proposal
from tests.test_transaction_balance_report import evidence
from tests.test_transaction_ops_state_db import seed_config


@pytest.fixture
async def approved_credit(db, admin_user):
    actor = admin_user[0]
    source, target, _, mapping, _ = evidence()
    config = await seed_config(
        db,
        actor.tenant_id,
        actor,
        netsuite_account_id="6738075",
        subsidiary_id="1",
        mapping_json={
            **mapping.model_dump(mode="json"),
            "action_mode": "propose_actions",
            "solidus_refund_step_id": str(uuid4()),
        },
    )
    now = datetime.now(timezone.utc)
    source["read_at"] = target["observed_at"] = now.isoformat()
    ref = source["orders"][0]["number"]
    initial = await state.create_run(
        db, actor.tenant_id, config.id, RunCreate(evaluation_key="before-credit", order_references=[ref]), actor=actor
    )
    now = datetime.now(timezone.utc)
    source["read_at"] = target["observed_at"] = now.isoformat()
    token = await state.claim_run(db, actor.tenant_id, initial.id, now=now)
    report = build_report(source, target, initial.config_snapshot, mapping, now=now)
    finding = await state.record_finding(db, actor.tenant_id, initial.id, ref, report, lease_token=token, now=now)
    await state.finish_run(db, actor.tenant_id, initial.id, "done", lease_token=token, now=now)
    case = await case_service.get_case(db, actor.tenant_id, finding.report_json["case_id"])
    p = kind_proposal("credit")
    p.update(
        tenant_id=str(actor.tenant_id),
        config_id=str(config.id),
        case_id=str(case.id),
        order_reference=ref,
        scope=case.scope_json,
        source=source["orders"][0],
    )
    p["before"]["createdFrom"] = {"id": "200"}
    session = ChatSession(tenant_id=actor.tenant_id, user_id=actor.id)
    db.add(session)
    await db.flush()
    message = ChatMessage(
        tenant_id=actor.tenant_id,
        session_id=session.id,
        role="assistant",
        content="",
        structured_output={
            "status": "approved",
            "accounting_review": p,
            "accounting_verification": {"status": "verified", "credit_memo_id": "30"},
        },
    )
    db.add(message)
    await db.flush()
    return actor, config, case, message, source, target


async def test_queue_is_durable_idempotent_and_scoped(db, approved_credit, tenant_b):
    actor, _, case, message, _, _ = approved_credit
    now = datetime.now(timezone.utc)
    run = await accounting_recheck.queue(db, actor.tenant_id, message, actor.id, now=now)
    assert (await accounting_recheck.queue(db, actor.tenant_id, message, actor.id, now=now)).id == run.id
    assert run.origin == "recovery" and run.max_orders == 1 and run.max_api_calls <= 64
    assert run.params_json["approved_by"] == str(actor.id)
    from app.services.transaction_ops.scheduler import _recovery_ids

    assert run.id in await _recovery_ids(db, actor.tenant_id, now)
    with pytest.raises(state.StateError, match="requires_verified"):
        await accounting_recheck.queue(db, tenant_b.id, message, actor.id, now=now)
    assert case.status == "open", "A verified credit is not yet full-case matching"


@pytest.mark.parametrize(
    "variant,expected",
    [
        ("match", "succeeded"),
        ("penny", "difference"),
        ("refund_missing", "unverified"),
        ("stale", "unverified"),
        ("wrong_target", "unverified"),
    ],
)
async def test_normal_runner_reconciles_after_credit_without_another_proposal_or_write(
    db, approved_credit, variant, expected
):
    actor, _, case, message, source, target = approved_credit
    queued_at = datetime.now(timezone.utc)
    run = await accounting_recheck.queue(db, actor.tenant_id, message, actor.id, now=queued_at)
    await db.commit()
    now = datetime.now(timezone.utc)
    observed = now if variant != "stale" else queued_at - timedelta(seconds=1)
    source["read_at"] = target["observed_at"] = observed.isoformat()
    target["orders"][0]["header"].update(total="120.00", taxTotal="20.01" if variant == "penny" else "20.00")
    if variant == "wrong_target":
        target["orders"][0]["record_id"] = target["orders"][0]["header"]["id"] = "201"
    refund = {"complete": True, "amount": "0.00", "currency": "USD", "order_reference": case.order_reference}
    guard = AsyncMock(side_effect=AssertionError("Read-only recheck cannot prepare or execute a write"))
    result = await run_investigation(
        db,
        actor.tenant_id,
        run.id,
        _clock=lambda: now,
        _enabled=AsyncMock(return_value=True),
        _source_reader=AsyncMock(return_value=source),
        _target_reader=AsyncMock(return_value=target),
        _source_refunds_reader=AsyncMock(return_value=refund),
        _target_refunds_reader=AsyncMock(return_value={"complete": False} if variant == "refund_missing" else refund),
        _guard_reader=guard,
        _celigo_reader=guard,
        _create_reader=guard,
    )
    assert result["termination_reason"] == "done"
    run = await state.get_run(db, actor.tenant_id, run.id)
    assert run.progress_json["settlement"]["status"] == expected
    assert run.progress_json["settlement"]["approved_by"] == str(actor.id)
    assert not await state.list_proposals(db, actor.tenant_id, run_id=run.id)
    guard.assert_not_awaited()
    if variant == "match":
        assert (await case_service.get_case(db, actor.tenant_id, case.id)).status == "reconciled"
    if variant != "match":
        assert (await case_service.get_case(db, actor.tenant_id, case.id)).status == "open"
    audits = list(
        await db.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == actor.tenant_id,
                AuditEvent.resource_id == str(run.id),
                AuditEvent.action == "transaction_ops.accounting_recheck.complete",
            )
        )
    )
    assert len(audits) == 1 and audits[0].payload["status"] == expected
