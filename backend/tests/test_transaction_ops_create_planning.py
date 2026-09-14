"""Only complete missing-order evidence can produce a native human proposal."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.transaction_ops import TransactionOperation
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import planner
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.normalization import TransactionMapping
from app.services.transaction_ops.runner import build_report, run_investigation
from tests.conftest import enable_feature_flag
from tests.test_transaction_ops_create_inputs import prepare
from tests.test_transaction_ops_create_transport import native_case
from tests.test_transaction_ops_state_db import seed_config


def missing_case(line_count=1):
    case = native_case()
    if line_count > 1:
        order = case.source["orders"][0]
        first = deepcopy(order["line_items"][0])
        order["line_items"] = []
        for index in range(line_count):
            line = deepcopy(first)
            line.update(
                id=str(11 + index),
                inventory_units=[{"id": str(501 + index), "shipment_id": "50", "state": "on_hand"}],
            )
            line["adjustments"][0].update(id=str(100 + index), adjustable_id=str(11 + index))
            order["line_items"].append(line)
        order["item_total"] = str(100 * line_count)
        for key in ("total", "payment_total", "order_total_after_store_credit"):
            order[key] = str(120 * line_count)
        for key in ("tax_total", "additional_tax_total", "adjustment_total"):
            order[key] = str(20 * line_count)
        order["payments"][0]["amount"] = str(120 * line_count)
        case.prepared = prepare(case)
        native = case.preview["record"]
        native["lines"] = [
            {**deepcopy(native["lines"][0]), "inventory_unit_ids": [str(501 + index)]} for index in range(line_count)
        ]
        native["body"].update(
            subtotal=str(100 * line_count),
            taxtotal=str(20 * line_count),
            total=str(120 * line_count),
            custbody_fw_solidus_order_total=str(120 * line_count),
            custbody_fw_solidus_tax_amount=str(20 * line_count),
        )
    case.targets["orders"] = []
    scope = {key: getattr(case.config, key) for key in ("netsuite_account_id", "subsidiary_id", "record_type")}
    case.report = build_report(
        case.source, case.targets, scope, TransactionMapping.model_validate(case.config.mapping_json), now=case.now
    )
    assert case.report["comparison"]["recommended_action"] == "propose_missing_sync"
    case.guard = {"preview": case.preview, "create_enabled": True, "observed_at": case.now.isoformat()}
    return case


def plan(case):
    return planner.plan_proposal(
        case.report, case.targets, case.config, now=case.now, guard=case.guard, creation=case.prepared
    )


def test_complete_native_create_proposal_binds_private_inputs_and_has_no_destination_id():
    case = missing_case()
    request = plan(case)
    assert request.action == "sync_missing_order" and request.target_record_id is None
    assert request.before_json == {"missing": True, "order_reference": "R123456789"}
    assert request.after_json == {"input": case.prepared.payload_json, "preview": case.preview}
    assert request.evidence_json["creation"]["private_source_fingerprint"] == case.prepared.private_fingerprint
    assert request.evidence_json["source_fingerprint"] == case.prepared.source_fingerprint
    assert "buyer@example.invalid" not in str(request.evidence_json)


def test_private_payment_identity_change_invalidates_approval_even_when_native_input_is_identical():
    case = missing_case()
    original = plan(case)
    case.source["orders"][0]["payments"][0]["id"] = "81"
    changed = prepare(case)
    assert changed.payload_json == case.prepared.payload_json
    case.prepared = changed
    assert plan(case).evidence_fingerprint != original.evidence_fingerprint


def test_creation_observation_refresh_does_not_change_economic_approval_fingerprint():
    case = missing_case()
    original = plan(case)
    case.now += timedelta(seconds=1)
    case.guard["observed_at"] = case.now.isoformat()
    assert plan(case).evidence_fingerprint == original.evidence_fingerprint


@pytest.mark.parametrize("kind", ["missing input", "disabled", "stale", "wrong currency", "changed source"])
def test_creation_cannot_be_planned_without_fresh_exact_native_and_source_proof(kind):
    case = missing_case()
    if kind == "missing input":
        case.prepared = None
    if kind == "disabled":
        case.guard["create_enabled"] = False
    if kind == "stale":
        case.guard["observed_at"] = (case.now - timedelta(minutes=16)).isoformat()
    if kind == "wrong currency":
        case.guard["preview"]["record"]["body"]["currency"] = "1"
    if kind == "changed source":
        case.report["source"]["updated_at"] = case.now.isoformat()
    with pytest.raises(planner.PlanningError):
        plan(case)


async def test_investigation_spends_before_unsaved_preview_and_persists_only_a_pending_proposal(db, admin_user):
    actor, _ = admin_user
    case = missing_case()
    config = await seed_config(db, actor.tenant_id, actor, subsidiary_id="3", mapping_json=case.config.mapping_json)
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="create-planning", order_references=["R123456789"]),
        actor=actor,
    )
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)

    async def preview(db, tenant, config, payload):
        current = await state.get_run(db, tenant, run.id)
        assert current.api_calls_used == 16
        assert payload == case.prepared.payload_json
        return case.guard

    source = AsyncMock(return_value=case.source)
    result = await run_investigation(
        db,
        actor.tenant_id,
        run.id,
        _source_reader=source,
        _target_reader=AsyncMock(return_value=case.targets),
        _create_reader=preview,
    )
    assert result["termination_reason"] == "done"
    assert source.await_args.kwargs == {"include_sync_data": True}
    proposals = await state.list_proposals(db, actor.tenant_id, run_id=run.id)
    assert len(proposals) == 1 and proposals[0].action == "sync_missing_order" and proposals[0].status == "pending"
    assert (
        not (await db.execute(select(TransactionOperation).where(TransactionOperation.tenant_id == actor.tenant_id)))
        .scalars()
        .all()
    )
    findings = await state.list_findings(db, actor.tenant_id, run.id)
    assert "buyer@example.invalid" not in str(findings[0].report_json)
    assert "Example Street" not in str(findings[0].report_json)
