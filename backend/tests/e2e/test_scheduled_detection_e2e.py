"""Real collector, runner and durable cases; only provider/broker edges synthetic."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.connection import Connection
from app.models.transaction_ops import TransactionOperation, TransactionProposal, TransactionRun
from app.schemas.transaction_runs import ConfigControl
from app.services.transaction_ops import case_service, runner, scheduler
from app.services.transaction_ops import state_service as state
from tests.conftest import enable_feature_flag
from tests.test_context_provenance import approve, propose
from tests.test_transaction_balance_report import evidence
from tests.test_transaction_ops_state_db import seed_config


def provider_evidence(now, variant):
    source, target, _, _, _ = evidence()
    source["read_at"] = now.isoformat()
    source["orders"][0].update(
        business_entity="Synthetic Company",
        state="complete",
        requires_review=False,
        updated_at=(now - timedelta(minutes=1)).isoformat(),
    )
    target["scope"] = {"account_id": "1234567-sb1", "subsidiary_id": "5"}
    target["observed_at"] = now.isoformat()
    target["orders"][0]["header"].update(total="120.00", taxTotal="20.00", subsidiary={"id": "5"})
    target["orders"][0]["version"] = now.isoformat()
    if variant == "missing":
        target["orders"] = []
        target["lookup"].update(count=0, complete=True)
    if variant == "timing":
        target["observed_at"] = (now - timedelta(minutes=2)).isoformat()
        target["orders"][0]["version"] = (now - timedelta(minutes=3)).isoformat()
    return source, target


@pytest.mark.parametrize(
    "variant,expected,cases",
    [("missing", "observed_missing", 1), ("timing", "timing_difference", 1), ("matched", "no_discrepancy", 0)],
)
async def test_collector_to_case_records_distinct_outcomes_and_replay(
    db, admin_user, monkeypatch, variant, expected, cases
):
    actor = admin_user[0]
    now = datetime.now(timezone.utc)
    mapping = {
        "reference_field": "tranid",
        "currency_minor_units": {"USD": 2},
        "business_entity_subsidiaries": {"Synthetic Company": "5"},
        "solidus_refund_step_id": str(uuid4()),
        "action_mode": "detect_only",
    }
    config = await seed_config(db, actor.tenant_id, actor, netsuite_account_id="1234567_SB1", mapping_json=mapping)
    connection = await db.scalar(select(Connection).where(Connection.id == config.netsuite_connection_id))
    connection.metadata_json = {"account_id": "1234567_SB1"}
    await db.flush()
    await propose(db, actor, config)
    approved = await approve(db, actor, config)
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
    await state.control_config(
        db, actor.tenant_id, config.id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(scheduler, "_dispatch", dispatch)
    monkeypatch.setattr(scheduler, "_refresh_sources", AsyncMock(return_value=0))
    stats = await scheduler.collect_due_runs(db, now)
    assert stats["created"] == 1
    run_id = dispatch.call_args.args[1]
    run = await state.get_run(db, actor.tenant_id, run_id)
    assert run.origin == "schedule"
    source, target = provider_evidence(now, variant)
    reference = source["orders"][0]["number"]
    page = {"page_complete": True, "page": 1, "total_count": 1, "orders": deepcopy(source["orders"]), "next_page": None}
    refund = {"order_reference": reference, "currency": "USD", "complete": True, "amount": "0.00"}
    result = await runner.run_investigation(
        db,
        actor.tenant_id,
        run_id,
        _clock=lambda: now,
        _page_reader=AsyncMock(return_value=page),
        _source_reader=AsyncMock(return_value=source),
        _target_reader=AsyncMock(return_value=target),
        _source_refunds_reader=AsyncMock(return_value=refund),
        _target_refunds_reader=AsyncMock(return_value=refund),
        _refund_page_reader=AsyncMock(return_value={"page_complete": True, "orders": [], "next_after_id": None}),
    )
    assert result["termination_reason"] == "done"
    assert result["matched"] == (1 if expected == "no_discrepancy" else 0)
    findings = await state.list_findings(db, actor.tenant_id, run_id)
    assert len(findings) == 1
    receipt = findings[0].report_json["scheduled_detection"]
    assert receipt["outcome"] == expected
    assert receipt["accounting_context"]["version"] == approved["version"] == 2
    assert receipt["accounting_context"]["audit_id"] == approved["audit_id"]
    assert receipt["accounting_context"]["policy_applied"] is False
    assert "Synthetic reviewed policy example" not in str(receipt)
    assert len(await case_service.list_cases(db, actor.tenant_id)) == cases
    await runner.run_investigation(db, actor.tenant_id, run_id)
    again = await scheduler.collect_due_runs(db, now)
    assert again["created"] == 0
    assert len((await db.scalars(select(TransactionRun).where(TransactionRun.tenant_id == actor.tenant_id))).all()) == 1
    assert len(await state.list_findings(db, actor.tenant_id, run_id)) == 1
    if cases:
        case = (await case_service.list_cases(db, actor.tenant_id))[0]
        assert len(await case_service.list_observations(db, actor.tenant_id, case.id)) == 1
    assert not (await db.scalars(select(TransactionProposal))).all()
    assert not (await db.scalars(select(TransactionOperation))).all()
