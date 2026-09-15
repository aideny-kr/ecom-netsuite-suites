"""Creation approval, independent pending-order proof and read-only recovery."""

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from app.models.transaction_ops import TransactionOperation
from app.schemas.transaction_runs import ProposalDecision, RunCreate
from app.services.transaction_ops import executor as mod
from app.services.transaction_ops import planner, recovery
from app.services.transaction_ops import state_service as state
from tests.conftest import enable_feature_flag
from tests.test_transaction_ops_create_planning import missing_case
from tests.test_transaction_ops_planner import planning_case
from tests.test_transaction_ops_state_db import seed_config


@pytest.fixture
async def create_execution_case(db, admin_user, monkeypatch, request):
    actor, _ = admin_user
    case = missing_case(getattr(request, "param", 1))
    config = await seed_config(db, actor.tenant_id, actor, subsidiary_id="3", mapping_json=case.config.mapping_json)
    case.config = config
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="create-execution", order_references=["R123456789"]),
        actor=actor,
    )
    token = await state.claim_run(db, actor.tenant_id, run.id)
    request = planner.plan_proposal(
        case.report, case.targets, config, now=case.now, guard=case.guard, creation=case.prepared
    )
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
    after = planning_case(inventory=True, assessment=True).targets
    version = case.now.isoformat()
    native = after["orders"][0]
    native.update(version=version)
    native["header"].update(
        orderStatus={"id": "A"},
        lastModifiedDate=version,
        total="120",
        subtotal="100",
        taxTotal="20",
        custbody_fw_solidus_order_total="120",
        custbody_fw_solidus_tax_amount="20",
    )
    native["lines"][0].update(quantity="2", rate="50", amount="100", custcol_fw_vat_amount="20", tax1Amt="20")
    count = len(case.prepared.payload_json["lines"])
    native["header"].update(
        total=str(120 * count),
        subtotal=str(100 * count),
        taxTotal=str(20 * count),
        custbody_fw_solidus_order_total=str(120 * count),
        custbody_fw_solidus_tax_amount=str(20 * count),
    )
    native["lines"] = [
        {**deepcopy(native["lines"][0]), "line": str(index + 1), "custcol_fw_inventory_unit_ids": str(501 + index)}
        for index in range(count)
    ]
    observed = {
        "observed_at": case.now.isoformat(),
        "creation": {
            "record_id": "63",
            "version": version,
            "work_key": proposal.work_key,
            "tax_profile": {"mode": "line_tax_amount", "tax_code_id": "610"},
            "inventory_mode": "line_location",
            "record": deepcopy(case.preview["record"]),
        },
    }
    case.source_reader = AsyncMock(return_value=case.source)
    case.target_reader = AsyncMock(side_effect=[case.targets, after])
    case.preview_reader = AsyncMock(return_value=case.guard)
    case.created_reader = AsyncMock(return_value=observed)

    async def dispatch(db, tenant, claimed):
        assert await state.reserve_operation_dispatch(
            db, tenant, claimed, provider="netsuite", payload_fingerprint="d" * 64
        )
        return {"status": "accepted", "record_id": "63", "verified": False}

    case.dispatch = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(mod, "read_framework_order", case.source_reader)
    monkeypatch.setattr(mod, "read_netsuite_order", case.target_reader)
    monkeypatch.setattr(mod, "read_create_preview", case.preview_reader, raising=False)
    monkeypatch.setattr(mod, "read_created_snapshot", case.created_reader, raising=False)
    monkeypatch.setattr(mod, "dispatch_netsuite_operation", case.dispatch)
    return SimpleNamespace(actor=actor, case=case, proposal=proposal, after=after, observed=observed)


async def operation(db, value):
    return (
        await db.execute(select(TransactionOperation).where(TransactionOperation.proposal_id == value.proposal.id))
    ).scalar_one()


async def execute(db, value):
    return await mod.execute_proposal(db, value.actor.tenant_id, value.proposal.id)


async def test_exact_creation_verifies_pending_approval_and_explicit_unit_conversion_once(db, create_execution_case):
    value = create_execution_case
    assert (await execute(db, value))["status"] == "verified"
    row = await operation(db, value)
    proof = row.result_json["verification"]
    assert proof["source_unchanged"] is True and proof["private_source_unchanged"] is True
    assert proof["report"]["targets"][0]["status"] == "draft"
    assert proof["report"]["targets"][0]["lines"][0]["quantity"] == "2"
    assert proof["creation_comparison"]["recommended_action"] == "no_action"
    assert row.api_calls_used == 33
    assert (await execute(db, value))["status"] == "verified"
    value.case.dispatch.assert_awaited_once()
    value.case.created_reader.assert_awaited_once()
    assert all(call.kwargs == {"include_sync_data": True} for call in value.case.source_reader.await_args_list)


@pytest.mark.parametrize("kind", ["address", "payment identity", "source version", "native FX", "already exists"])
async def test_changed_creation_evidence_fails_before_dispatch(db, create_execution_case, kind):
    value = create_execution_case
    if kind == "address":
        value.case.source["orders"][0]["ship_address"]["address1"] = "Changed"
    if kind == "payment identity":
        value.case.source["orders"][0]["payments"][0]["id"] = "81"
    if kind == "source version":
        value.case.source["orders"][0]["updated_at"] = datetime.now(timezone.utc).isoformat()
    if kind == "native FX":
        value.case.guard["preview"]["record"]["body"]["exchangerate"] = "1.2"
    if kind == "already exists":
        value.case.target_reader.side_effect = [value.after]
    assert (await execute(db, value))["status"] == "failed"
    value.case.dispatch.assert_not_awaited()
    assert not (await operation(db, value)).result_json.get("dispatch_reserved")


@pytest.mark.parametrize("kind", ["work key", "native state", "amount", "quantity", "duplicate", "version", "address"])
async def test_unproven_created_order_remains_unknown_without_another_send(db, create_execution_case, kind):
    value = create_execution_case
    native = value.after["orders"][0]
    if kind == "work key":
        value.observed["creation"]["work_key"] = "a" * 64
    if kind == "native state":
        native["header"]["orderStatus"] = {"id": "B"}
    if kind == "amount":
        native["header"]["total"] = "121"
    if kind == "quantity":
        native["lines"][0]["quantity"] = "1"
    if kind == "duplicate":
        value.after["orders"].append(deepcopy(native))
    if kind == "version":
        value.observed["creation"]["version"] = (value.case.now - timedelta(seconds=1)).isoformat()
    if kind == "address":
        changed = deepcopy(value.case.source)
        changed["orders"][0]["ship_address"]["address1"] = "Changed after save"
        value.case.source_reader.side_effect = [value.case.source, changed]
    assert (await execute(db, value))["status"] == "unknown"
    assert (await execute(db, value))["status"] == "unknown"
    value.case.dispatch.assert_awaited_once()


async def test_unknown_creation_receipt_is_verified_only_by_independent_attributed_reads(db, create_execution_case):
    value = create_execution_case
    original = value.case.dispatch.side_effect

    async def lost_receipt(*args):
        await original(*args)
        return {"status": "unknown", "verified": False}

    value.case.dispatch.side_effect = lost_receipt
    assert (await execute(db, value))["status"] == "verified"
    value.case.dispatch.assert_awaited_once()


async def test_recovery_of_lost_creation_checks_attribution_and_never_constructs_or_sends_another_draft(
    db, create_execution_case, monkeypatch
):
    value = create_execution_case
    value.case.created_reader.side_effect = RuntimeError("Read temporarily unavailable")
    assert (await execute(db, value))["status"] == "unknown"
    row = await operation(db, value)
    spent = row.api_calls_used
    monkeypatch.setattr(recovery, "read_framework_order", AsyncMock(return_value=value.case.source))
    monkeypatch.setattr(recovery, "read_netsuite_order", AsyncMock(return_value=value.after))
    read_created = AsyncMock(return_value=value.observed)
    monkeypatch.setattr(recovery, "read_created_snapshot", read_created, raising=False)
    assert (await recovery.recover_operation(db, value.actor.tenant_id, row.id))["status"] == "verified"
    read_created.assert_awaited_once()
    value.case.preview_reader.assert_awaited_once()
    value.case.dispatch.assert_awaited_once()
    assert (await operation(db, value)).api_calls_used == spent


@pytest.mark.parametrize("create_execution_case", [50, 75], indirect=True)
@pytest.mark.parametrize("recover", [False, True])
async def test_large_approved_creation_persists_bounded_proof_without_losing_native_observations(
    db, create_execution_case, monkeypatch, recover
):
    value = create_execution_case
    if recover:
        value.case.created_reader.side_effect = RuntimeError("Temporary read failure")
        assert (await execute(db, value))["status"] == "unknown"
        row = await operation(db, value)
        spent = row.api_calls_used
        monkeypatch.setattr(recovery, "read_framework_order", AsyncMock(return_value=value.case.source))
        monkeypatch.setattr(recovery, "read_netsuite_order", AsyncMock(return_value=value.after))
        monkeypatch.setattr(recovery, "read_created_snapshot", AsyncMock(return_value=value.observed))
        result = await recovery.recover_operation(db, value.actor.tenant_id, row.id)
        assert (await operation(db, value)).api_calls_used == spent
    else:
        result = await execute(db, value)
    assert result["status"] == "verified"
    proof = (await operation(db, value)).result_json["verification"]
    assert proof["evidence_retention"] == "summary_and_digests"
    assert len(json.dumps(proof).encode()) <= 48 * 1024
    assert proof["evidence_fingerprint"] == value.proposal.evidence_fingerprint
    assert proof["source_fingerprint"] == value.case.prepared.source_fingerprint
    assert proof["private_source_fingerprint"] == value.case.prepared.private_fingerprint
    assert proof["guard"]["record_fingerprint"] == state.business_digest(value.observed["creation"]["record"])
    assert proof["guard"]["record_fingerprint"] == proof["guard"]["approved_record_fingerprint"]
    assert proof["lookup"]["complete"] is True and proof["lookup"]["authoritative"] is True
    observations = proof["line_observations"]
    assert len(observations) == len(value.case.prepared.payload_json["lines"])
    assert all(line["source_quantity"] == "1" and line["native_quantity"] == "2" for line in observations)
    assert proof["target_observation"]["status"] == "draft"
    assert proof["creation_policy"]["native_order_status"] == "A"
    value.case.dispatch.assert_awaited_once()
    value.case.preview_reader.assert_awaited_once()
    if recover and len(observations) == 75:
        run_id = (await operation(db, value)).result_json["recovery"]["run_id"]
        finding = (await state.list_findings(db, value.actor.tenant_id, UUID(run_id)))[0]
        assert finding.report_json["evidence_limits"]["target_line_counts"] == [75]


@pytest.mark.parametrize("create_execution_case", [75], indirect=True)
async def test_large_creation_does_not_summarize_away_a_post_save_money_mismatch(db, create_execution_case):
    value = create_execution_case
    value.after["orders"][0]["header"]["total"] = "9001"
    assert (await execute(db, value))["status"] == "unknown"
    value.case.dispatch.assert_awaited_once()
