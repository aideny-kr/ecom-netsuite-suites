from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.transaction_ops import TransactionOperation
from app.schemas.transaction_runs import ProposalDecision, RunCreate
from app.services.transaction_ops import executor as mod
from app.services.transaction_ops import planner
from app.services.transaction_ops import state_service as state
from tests.conftest import enable_feature_flag
from tests.test_transaction_ops_planner import planning_case
from tests.test_transaction_ops_state_db import seed_config


@pytest.fixture
async def execution_case(db, admin_user, monkeypatch, request):
    actor, _ = admin_user
    case = planning_case(inventory=getattr(request, "param", False))
    config = await seed_config(db, actor.tenant_id, actor, subsidiary_id="3", mapping_json=case.config.mapping_json)
    case.config = config
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="execute", order_references=["R123456789"]),
        actor=actor,
    )
    token = await state.claim_run(db, actor.tenant_id, run.id)
    request = planner.plan_proposal(case.report, case.targets, config, now=case.now, guard=case.guard)
    proposal = await state.propose(db, actor.tenant_id, run.id, request, lease_token=token)
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
    await state.decide_proposal(
        db,
        actor.tenant_id,
        proposal.id,
        ProposalDecision(decision="approve", evidence_fingerprint=proposal.evidence_fingerprint),
        actor=actor,
    )
    await state.finish_run(db, actor.tenant_id, run.id, "done", lease_token=token)
    before = deepcopy(case.targets)
    after = deepcopy(before)
    after["orders"][0]["header"].update(total="100", subtotal="100", custbody_fw_solidus_order_total="100")
    after["orders"][0]["lines"][0].update(rate="100", amount="100")
    after_guard = deepcopy(case.guard)
    after_guard["snapshot"].update(total="100", subtotal="100", custbody_fw_solidus_order_total="100")
    after_guard["snapshot"]["lines"][0].update(rate="100", amount="100")
    case.read_source = AsyncMock(return_value=case.source)
    case.read_target = AsyncMock(side_effect=[before, after])
    case.read_guard = AsyncMock(side_effect=[case.guard, after_guard])

    async def dispatch(db, tenant, claimed):
        assert await state.reserve_operation_dispatch(
            db, tenant, claimed, provider="netsuite", payload_fingerprint="d" * 64
        )
        return {"status": "accepted", "verified": False, "record_id": "63"}

    case.dispatch = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(mod, "read_framework_order", case.read_source)
    monkeypatch.setattr(mod, "read_netsuite_order", case.read_target)
    monkeypatch.setattr(mod, "read_guard_snapshot", case.read_guard)
    monkeypatch.setattr(mod, "dispatch_netsuite_operation", case.dispatch)
    return SimpleNamespace(
        actor=actor, case=case, proposal=proposal, before=before, after=after, after_guard=after_guard
    )


async def operation(db, case):
    return (
        await db.execute(select(TransactionOperation).where(TransactionOperation.proposal_id == case.proposal.id))
    ).scalar_one()


async def execute(db, case):
    return await mod.execute_proposal(db, case.actor.tenant_id, case.proposal.id)


async def test_human_approval_runs_once_and_independent_reads_verify_changes(db, execution_case):
    result = await execute(db, execution_case)
    assert result["status"] == "verified" and result["termination_reason"] == "done"
    case = execution_case.case
    case.dispatch.assert_awaited_once()
    assert case.read_source.await_count == 2 and case.read_target.await_count == 2 and case.read_guard.await_count == 2
    row = await operation(db, execution_case)
    assert row.result_json["dispatch_reserved"] is True and row.result_json["verification"]["source_unchanged"] is True
    assert row.api_calls_used == 33  # Two source/target/guard reads plus one dispatch.
    assert (await execute(db, execution_case))["status"] == "verified"
    case.dispatch.assert_awaited_once()
    assert case.read_source.await_count == 2


@pytest.mark.parametrize("change", ["source_amount", "source_version", "target_version", "target_currency"])
async def test_changed_approved_evidence_stops_before_every_external_write(db, execution_case, change):
    case = execution_case.case
    if change.startswith("source"):
        case.source = deepcopy(case.source)
        case.source["orders"][0]["total" if change == "source_amount" else "updated_at"] = (
            "101" if change == "source_amount" else datetime.now(timezone.utc).isoformat()
        )
        case.read_source.return_value = case.source
    elif change == "target_version":
        execution_case.before["orders"][0].update(version="2026-09-05T01:00:00Z")
        execution_case.before["orders"][0]["header"]["lastModifiedDate"] = "2026-09-05T01:00:00Z"
    else:
        execution_case.before["orders"][0]["currency_metadata"]["symbol"] = "USD"
    result = await execute(db, execution_case)
    assert result["status"] == "failed"
    case.dispatch.assert_not_awaited()
    assert not (await operation(db, execution_case)).result_json.get("dispatch_reserved")


async def test_success_receipt_without_changed_provider_state_stays_unknown(db, execution_case):
    case = execution_case.case
    case.read_target.side_effect = [execution_case.before, execution_case.before]
    case.read_guard.side_effect = [case.guard, case.guard]
    result = await execute(db, execution_case)
    assert result["status"] == "unknown"
    assert (await operation(db, execution_case)).result_json["code"] == "verification_unproven"
    case.dispatch.assert_awaited_once()


async def test_uncertain_receipt_can_be_verified_by_independent_authoritative_reads(db, execution_case):
    case = execution_case.case
    previous = case.dispatch.side_effect

    async def lost_response(*args):
        await previous(*args)
        return {"status": "unknown", "verified": False}

    case.dispatch.side_effect = lost_response
    assert (await execute(db, execution_case))["status"] == "verified"


async def test_exception_after_reservation_is_unknown_and_does_not_leak_payload(db, execution_case):
    case = execution_case.case
    previous = case.dispatch.side_effect

    async def crashed(*args):
        await previous(*args)
        raise RuntimeError("private billing address token")

    case.dispatch.side_effect = crashed
    result = await execute(db, execution_case)
    assert result["status"] == "unknown"
    assert "private billing" not in str((await operation(db, execution_case)).result_json)
    assert (await execute(db, execution_case))["status"] == "unknown"
    case.dispatch.assert_awaited_once()


async def test_unapproved_or_expired_proposal_never_reads_or_sends(db, execution_case):
    case = execution_case.case
    # A clock beyond the immutable approval validity expires it at claim.
    result = await mod.execute_proposal(
        db,
        execution_case.actor.tenant_id,
        execution_case.proposal.id,
        _clock=lambda: execution_case.proposal.valid_until + timedelta(seconds=1),
    )
    assert result["status"] == "superseded"
    case.dispatch.assert_not_awaited()
    case.read_source.assert_not_awaited()


async def test_postwrite_source_change_is_not_called_verified(db, execution_case):
    case = execution_case.case
    changed = deepcopy(case.source)
    changed["orders"][0]["updated_at"] = datetime.now(timezone.utc).isoformat()
    case.read_source.side_effect = [case.source, changed]
    assert (await execute(db, execution_case))["status"] == "unknown"


async def test_whole_guard_state_must_match_after_so_customer_drift_is_visible(db, execution_case):
    execution_case.after_guard["snapshot"]["entity"] = "999"
    assert (await execute(db, execution_case))["status"] == "unknown"


@pytest.mark.parametrize("execution_case", [True], indirect=True)
async def test_inventory_approval_reloads_private_source_for_both_execution_reads(db, execution_case):
    result = await execute(db, execution_case)
    assert result["status"] == "verified"
    calls = execution_case.case.read_source.await_args_list
    assert len(calls) == 2 and all(call.kwargs == {"include_sync_data": True} for call in calls)
    execution_case.case.dispatch.assert_awaited_once()


@pytest.mark.parametrize("execution_case", [True], indirect=True)
@pytest.mark.parametrize("change", ["source_inventory", "source_sku", "target_inventory"])
async def test_inventory_drift_after_approval_prevents_dispatch(db, execution_case, change):
    case = execution_case.case
    if change == "source_inventory":
        case.source["orders"][0]["line_items"][0]["inventory_units"] = [{"id": "999"}]
    elif change == "source_sku":
        case.source["orders"][0]["line_items"][0]["variant"]["sku"] = "OTHER"
    else:
        execution_case.before["orders"][0]["lines"][0]["custcol_fw_inventory_unit_ids"] = "999"
    assert (await execute(db, execution_case))["status"] == "failed"
    case.dispatch.assert_not_awaited()
