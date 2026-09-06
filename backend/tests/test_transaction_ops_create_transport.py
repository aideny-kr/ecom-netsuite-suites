"""Native draft contract and one-send creation transport with real approval state."""

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.core.encryption import encrypt_credentials
from app.models.connection import Connection
from app.models.transaction_ops import TransactionOperation
from app.schemas.transaction_runs import ProposalDecision, RunCreate
from app.services.transaction_ops import netsuite_create as create
from app.services.transaction_ops import netsuite_transport as mod
from app.services.transaction_ops import state_service as state
from tests.conftest import enable_feature_flag
from tests.test_transaction_ops_create_inputs import create_case, prepare
from tests.test_transaction_ops_netsuite_dispatch import URL
from tests.test_transaction_ops_state_db import new_proposal, seed_config


def native_case():
    case = create_case()
    case.prepared = prepare(case)
    path = Path(__file__).resolve().parents[2] / "suiteapp/__tests__/fixtures/transaction_create_preview.json"
    case.preview = json.loads(path.read_text())
    case.preview["record"]["body"]["trandate"] = case.prepared.payload_json["transaction_date"]
    return case


def preview_response(case):
    return {
        "schema_version": 1,
        "success": True,
        "account_id": "6738075_SB1",
        "create_enabled": True,
        "preview": deepcopy(case.preview),
    }


def test_native_javascript_preview_is_accepted_without_changing_approved_fields():
    case = native_case()
    result = create.validate_create_preview(case.prepared.payload_json, case.preview)
    assert result == case.preview
    result["record"]["body"]["total"] = "999"
    assert case.preview["record"]["body"]["total"] == "120"


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(schema_version=True),
        lambda p: p.update(extra="unknown"),
        lambda p: p.update(period_id="0"),
        lambda p: p["metadata"]["currency"].update(symbol="USD"),
        lambda p: p["metadata"]["currency"].update(precision=0),
        lambda p: p["metadata"]["customer"].update(credit_hold="ON"),
        lambda p: p["metadata"]["customer"].update(currency_ids=["1"]),
        lambda p: p["metadata"]["customer"].update(credit_limit="100"),
        lambda p: p["metadata"]["items"].append(deepcopy(p["metadata"]["items"][0])),
        lambda p: p["metadata"]["items"][0].update(type="SerializedInvtPart"),
        lambda p: p["metadata"]["locations"][0].update(subsidiary="2"),
        lambda p: p["record"]["body"].update(subsidiary="2"),
        lambda p: p["record"]["body"].update(currency="1"),
        lambda p: p["record"]["body"].update(orderstatus="B"),
        lambda p: p["record"]["body"].update(total="121"),
        lambda p: p["record"]["body"].update(exchangerate="0"),
        lambda p: p["record"]["body"].update(tobeemailed=True),
        lambda p: p["record"]["body"].update(tobeemailed=0),
        lambda p: p["record"]["body"].update(iscrosssubtransaction=0),
        lambda p: p["record"]["body"].update(discountitem="99"),
        lambda p: p["record"]["billing_address"].update(addr1="Changed address"),
        lambda p: p["record"]["lines"][0].update(quantity="1"),
        lambda p: p["record"]["lines"][0].update(quantityfulfilled="1"),
        lambda p: p["record"]["lines"][0].update(taxcode="999"),
        lambda p: p["record"]["lines"][0].update(inventory_unit_ids=["502"]),
        lambda p: p["record"]["lines"][0].update(location="5"),
        lambda p: p["metadata"]["customer"].update(currency_ids=["4", "4"]),
        lambda p: p["metadata"]["currency"].update(id=4),
    ],
)
def test_unknown_or_conflicting_native_preview_cannot_reach_human_approval(change):
    case = native_case()
    change(case.preview)
    with pytest.raises(create.CreateInputError):
        create.validate_create_preview(case.prepared.payload_json, case.preview)


@pytest.mark.parametrize("symbol,precision", [("JPY", 0), ("EUR", 2), ("KWD", 3)])
def test_native_draft_currency_uses_explicit_configured_precision(symbol, precision):
    case = native_case()
    payload = case.prepared.payload_json
    payload["currency"] = {"symbol": symbol, "precision": precision}
    case.preview["metadata"]["currency"].update(symbol=symbol, precision=precision)
    assert create.validate_create_preview(payload, case.preview) == case.preview


def test_aggregate_and_cross_subsidiary_native_projection_has_exact_header_and_line_fields():
    case = native_case()
    payload = case.prepared.payload_json
    payload["tax_profile"]["mode"] = "aggregate_header"
    payload["native_tax_rounding"] = "half_up"
    payload["inventory_mode"] = "cross_subsidiary"
    payload["lines"][0]["inventory_subsidiary_id"] = "2"
    case.preview["metadata"]["locations"][0]["subsidiary"] = "2"
    case.preview["record"]["body"].update(taxitem="610", taxrate="20", istaxable=True, iscrosssubtransaction=True)
    line = case.preview["record"]["lines"][0]
    for field in ("taxcode", "taxrate1", "tax1amt", "location"):
        del line[field]
    line.update(istaxable=True, inventorylocation="4", inventorysubsidiary="2")
    assert create.validate_create_preview(payload, case.preview) == case.preview
    case.preview["record"]["body"]["taxitem"] = "999"
    with pytest.raises(create.CreateInputError):
        create.validate_create_preview(payload, case.preview)


@pytest.fixture
async def create_dispatch_case(db, admin_user, monkeypatch):
    actor, _ = admin_user
    example = native_case()
    config = await seed_config(db, actor.tenant_id, actor, subsidiary_id="3", mapping_json=example.config.mapping_json)
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="native-create", order_references=["R123456789"]),
        actor=actor,
    )
    proposal = await new_proposal(
        db,
        actor,
        run,
        action="sync_missing_order",
        target_record_id=None,
        currency="EUR",
        before_json={"missing": True, "order_reference": "R123456789"},
        after_json={"input": example.prepared.payload_json, "preview": example.preview},
    )
    for name in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, name)
    await state.decide_proposal(
        db,
        actor.tenant_id,
        proposal.id,
        ProposalDecision(decision="approve", evidence_fingerprint=proposal.evidence_fingerprint),
        actor=actor,
    )
    claim = await state.claim_approved_operation(
        db, actor.tenant_id, proposal.id, expected_evidence_fingerprint=proposal.evidence_fingerprint
    )
    connection = (
        await db.execute(select(Connection).where(Connection.id == config.netsuite_connection_id))
    ).scalar_one()
    connection.encrypted_credentials = encrypt_credentials(
        {"account_id": "6738075_SB1", "access_token": "fixture-token"}
    )
    connection.metadata_json = {"transaction_ops_guard_url": URL}
    await db.flush()
    monkeypatch.setattr(mod, "get_valid_token", AsyncMock(return_value="fixture-token"))
    return SimpleNamespace(actor=actor, config=config, proposal=proposal, claim=claim, example=example)


def native_transport(case, *, preview_change=None, save_mode="saved", created_change=None):
    requests = []

    async def handler(request):
        payload = json.loads(request.content) if request.content else None
        requests.append((request, payload))
        assert request.url.params["script"] == "customscript_ecom_tx_ops_guard"
        if request.method == "GET":
            assert request.url.params["action"] == "created_snapshot"
            version = datetime.now(timezone.utc).isoformat()
            if created_change:
                version = created_change(version)
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "success": True,
                    "account_id": "6738075_SB1",
                    "creation": {
                        "record_id": "63",
                        "version": version,
                        "work_key": case.claim.work_key,
                        "tax_profile": {"mode": "line_tax_amount", "tax_code_id": "610"},
                        "inventory_mode": "line_location",
                        "record": case.example.preview["record"],
                    },
                },
            )
        if payload["action"] == "preview_create":
            result = preview_response(case.example)
            if preview_change:
                preview_change(result)
            return httpx.Response(200, json=result)
        assert payload["action"] == "sync_missing_order"
        assert payload["after"] == case.proposal.after_json
        row = (await db_operation(case)).result_json
        assert row["dispatch_reserved"] is True
        if save_mode == "timeout":
            raise httpx.ReadTimeout("private provider payload fixture-token")
        if save_mode == "rejected":
            return httpx.Response(
                200, json={"schema_version": 1, "success": False, "status": "rejected", "verified": False}
            )
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "success": True,
                "status": "saved",
                "record_id": "63" if save_mode == "saved" else "../foreign",
                "work_key": case.claim.work_key,
                "verified": False,
            },
        )

    return requests, httpx.MockTransport(handler)


async def db_operation(case):
    return (
        await case.db.execute(select(TransactionOperation).where(TransactionOperation.id == case.claim.operation_id))
    ).scalar_one()


@pytest.mark.parametrize(
    "mode,expected", [("saved", "accepted"), ("timeout", "unknown"), ("rejected", "failed"), ("bad_id", "unknown")]
)
async def test_creation_consumes_one_durable_send_and_never_treats_receipt_as_verified(
    db, create_dispatch_case, mode, expected
):
    case = create_dispatch_case
    case.db = db
    requests, transport = native_transport(case, save_mode=mode)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await mod.dispatch_netsuite_operation(db, case.actor.tenant_id, case.claim, client=client)
        again = await mod.dispatch_netsuite_operation(db, case.actor.tenant_id, case.claim, client=client)
    assert result["status"] == expected and result["verified"] is False
    assert again["status"] == "unknown"
    assert [p["action"] for _, p in requests] == ["preview_create", "sync_missing_order"]
    assert (await db_operation(case)).api_calls_used == mod.MAX_GUARD_READ_CALLS + 1


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(create_enabled=False),
        lambda r: r.update(account_id="9999999"),
        lambda r: r["preview"]["record"]["body"].update(exchangerate="1.2"),
        lambda r: r["preview"]["record"]["body"].update(total="121"),
    ],
)
async def test_changed_or_disabled_native_creation_never_reserves_a_send(db, create_dispatch_case, change):
    case = create_dispatch_case
    case.db = db
    requests, transport = native_transport(case, preview_change=change)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(mod.NetSuiteActionError):
            await mod.dispatch_netsuite_operation(db, case.actor.tenant_id, case.claim, client=client)
    assert len(requests) == 1
    assert not (await db_operation(case)).result_json.get("dispatch_reserved")


async def test_read_only_preview_and_created_snapshot_never_consume_the_write_permit(db, create_dispatch_case):
    case = create_dispatch_case
    case.db = db
    requests, transport = native_transport(case)
    async with httpx.AsyncClient(transport=transport) as client:
        preview = await mod.read_create_preview(
            db, case.actor.tenant_id, case.config, case.example.prepared.payload_json, client=client
        )
        created = await mod.read_created_snapshot(
            db, case.actor.tenant_id, case.config, "63", case.proposal.after_json, client=client
        )
    assert preview["preview"] == case.example.preview
    assert created["creation"]["work_key"] == case.claim.work_key
    assert [(r.method, p and p["action"]) for r, p in requests] == [("POST", "preview_create"), ("GET", None)]
    assert not (await db_operation(case)).result_json.get("dispatch_reserved")


@pytest.mark.parametrize("version", [None, 123, "2026-01-01T00:00:00", "2099-01-01T00:00:00Z"])
async def test_created_snapshot_requires_a_known_aware_nonfuture_native_version(db, create_dispatch_case, version):
    case = create_dispatch_case
    case.db = db
    requests, transport = native_transport(case, created_change=lambda _: version)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(mod.NetSuiteActionError, match="guard_created_snapshot_unavailable"):
            await mod.read_created_snapshot(
                db, case.actor.tenant_id, case.config, "63", case.proposal.after_json, client=client
            )
    assert len(requests) == 1 and requests[0][0].method == "GET"
    assert not (await db_operation(case)).result_json.get("dispatch_reserved")
