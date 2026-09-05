"""Real approval ledger with a bounded, entirely mocked NetSuite transport."""

from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.core.encryption import encrypt_credentials
from app.models.connection import Connection
from app.models.transaction_ops import TransactionOperation
from app.schemas.transaction_runs import ProposalDecision, RunCreate
from app.services.transaction_ops import netsuite_actions
from app.services.transaction_ops import netsuite_transport as mod
from app.services.transaction_ops import state_service as state
from tests import test_transaction_ops_netsuite_actions as examples
from tests.conftest import enable_feature_flag
from tests.test_transaction_ops_state_db import new_proposal, seed_config

URL = "https://6738075-sb1.restlets.api.netsuite.com/app/site/hosting/restlet.nl?script=customscript_ecom_tx_ops_guard&deploy=customdeploy_ecom_tx_ops_guard"


@pytest.fixture
async def dispatch_case(db, admin_user, monkeypatch):
    actor, _ = admin_user
    config = await seed_config(db, actor.tenant_id, actor, subsidiary_id="3")
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="guard-dispatch", order_references=["R123456789"]),
        actor=actor,
    )
    plan = netsuite_actions.prepare_correction(examples.target(), examples.source())
    proposal = await new_proposal(
        db, actor, run, before_json=plan.before_json, after_json=plan.after_json, target_record_id="63", currency="EUR"
    )
    await enable_feature_flag(db, actor.tenant_id, "celigo")
    await enable_feature_flag(db, actor.tenant_id, "reconciliation")
    await state.decide_proposal(
        db,
        actor.tenant_id,
        proposal.id,
        ProposalDecision(decision="approve", evidence_fingerprint=proposal.evidence_fingerprint),
        actor=actor,
    )
    claimed = await state.claim_approved_operation(
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
    loader = AsyncMock(wraps=mod._load_guard_credentials)
    monkeypatch.setattr(mod, "_load_guard_credentials", loader)
    return actor, proposal, claimed, loader


def transport(case, *, change_snapshot=None, post_status=200, post_body=None, timeout=False):
    _, _, claim, _ = case
    requests = []

    async def handler(request):
        requests.append(request)
        assert request.url.params["script"] == "customscript_ecom_tx_ops_guard"
        assert request.url.params["deploy"] == "customdeploy_ecom_tx_ops_guard"
        assert request.headers["authorization"] == "Bearer fixture-token"
        if request.method == "GET":
            snapshot = deepcopy(claim.before_json)
            if change_snapshot:
                change_snapshot(snapshot)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "schema_version": 1,
                    "account_id": "6738075_SB1",
                    "actions_enabled": True,
                    "snapshot": snapshot,
                },
            )
        if timeout:
            raise httpx.ReadTimeout("private payload and fixture-token")
        return httpx.Response(
            post_status,
            json=post_body
            or {
                "success": True,
                "schema_version": 1,
                "status": "saved",
                "record_id": "63",
                "work_key": claim.work_key,
                "verified": False,
            },
        )

    return requests, httpx.MockTransport(handler)


async def test_approved_exact_intent_is_sent_once_and_receipt_is_not_verification(db, dispatch_case):
    actor, proposal, claim, _ = dispatch_case
    requests, handler = transport(dispatch_case)
    async with httpx.AsyncClient(transport=handler) as client:
        result = await mod.dispatch_netsuite_operation(db, actor.tenant_id, claim, client=client)
        again = await mod.dispatch_netsuite_operation(db, actor.tenant_id, claim, client=client)
    assert result == {"status": "accepted", "record_id": "63", "verified": False}
    assert again["status"] == "unknown"
    writes = [r for r in requests if r.method == "POST"]
    assert len(writes) == 1
    import json

    payload = json.loads(writes[0].content)
    assert payload["before"] == proposal.before_json and payload["after"] == proposal.after_json
    assert payload["work_key"] == proposal.work_key
    operation = (
        await db.execute(select(TransactionOperation).where(TransactionOperation.id == claim.operation_id))
    ).scalar_one()
    assert operation.result_json["dispatch_reserved"] is True
    assert operation.status == "executing"
    assert operation.api_calls_used <= operation.max_api_calls


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.update(currency="9"),
        lambda s: s.update(entity="41"),
        lambda s: s["lines"][0].update(amount="999"),
        lambda s: s.update(version="changed"),
    ],
)
async def test_changed_guard_snapshot_prevents_dispatch(db, dispatch_case, change):
    actor, _, claim, _ = dispatch_case
    requests, handler = transport(dispatch_case, change_snapshot=change)
    async with httpx.AsyncClient(transport=handler) as client:
        with pytest.raises(mod.NetSuiteActionError, match="guard_evidence_changed"):
            await mod.dispatch_netsuite_operation(db, actor.tenant_id, claim, client=client)
    assert not [r for r in requests if r.method != "GET"]


@pytest.mark.parametrize("status", [302, 429, 500, 503])
async def test_mutation_http_errors_are_unknown_without_retry(db, dispatch_case, status):
    actor, _, claim, _ = dispatch_case
    requests, handler = transport(dispatch_case, post_status=status)
    async with httpx.AsyncClient(transport=handler, follow_redirects=True) as client:
        result = await mod.dispatch_netsuite_operation(db, actor.tenant_id, claim, client=client)
    assert result["status"] == "unknown" and result["verified"] is False
    assert len([r for r in requests if r.method == "POST"]) == 1


async def test_timeout_after_send_does_not_return_sensitive_transport_error(db, dispatch_case):
    actor, _, claim, _ = dispatch_case
    requests, handler = transport(dispatch_case, timeout=True)
    async with httpx.AsyncClient(transport=handler) as client:
        result = await mod.dispatch_netsuite_operation(db, actor.tenant_id, claim, client=client)
    assert result == {"status": "unknown", "code": "provider_write_outcome_unknown", "verified": False}
    assert "fixture-token" not in str(result)


async def test_guard_rejection_is_failed_only_when_no_save_was_attempted(db, dispatch_case):
    actor, _, claim, _ = dispatch_case
    requests, handler = transport(
        dispatch_case,
        post_body={
            "success": False,
            "schema_version": 1,
            "status": "rejected",
            "code": "evidence_changed",
            "verified": False,
        },
    )
    async with httpx.AsyncClient(transport=handler) as client:
        result = await mod.dispatch_netsuite_operation(db, actor.tenant_id, claim, client=client)
    assert result == {"status": "failed", "code": "guard_rejected", "verified": False}


async def test_stale_budget_stops_before_credentials_or_provider_reads(db, dispatch_case):
    actor, _, claim, loader = dispatch_case
    await state.reserve_operation_budget(db, actor.tenant_id, claim.operation_id, api_calls=96)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("unbudgeted request"))) as client:
        with pytest.raises(mod.NetSuiteActionError, match="operation_budget_exhausted"):
            await mod.dispatch_netsuite_operation(db, actor.tenant_id, claim, client=client)
    loader.assert_not_awaited()


async def test_duplicate_delivery_does_not_spend_or_read_again_after_dispatch(db, dispatch_case):
    actor, _, claim, _ = dispatch_case
    requests, handler = transport(dispatch_case)
    async with httpx.AsyncClient(transport=handler) as client:
        await mod.dispatch_netsuite_operation(db, actor.tenant_id, claim, client=client)
        used = (
            (await db.execute(select(TransactionOperation).where(TransactionOperation.id == claim.operation_id)))
            .scalar_one()
            .api_calls_used
        )
        await mod.dispatch_netsuite_operation(db, actor.tenant_id, claim, client=client)
    assert len(requests) == 2
    assert (
        await db.execute(select(TransactionOperation).where(TransactionOperation.id == claim.operation_id))
    ).scalar_one().api_calls_used == used


@pytest.mark.parametrize("change", ["revoked", "account", "url"])
async def test_credential_scope_fails_before_any_provider_request(db, dispatch_case, change):
    actor, _, claim, _ = dispatch_case
    config = await state.get_config(db, actor.tenant_id, claim.config_id)
    connection = (
        await db.execute(select(Connection).where(Connection.id == config.netsuite_connection_id))
    ).scalar_one()
    if change == "revoked":
        connection.status = "revoked"
    elif change == "account":
        connection.encrypted_credentials = encrypt_credentials({"account_id": "9999999"})
    else:
        connection.metadata_json = {"transaction_ops_guard_url": "https://untrusted.example/"}
    await db.flush()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("unscoped provider request"))
    ) as client:
        with pytest.raises(mod.NetSuiteActionError, match="guard_connection_unavailable"):
            await mod.dispatch_netsuite_operation(db, actor.tenant_id, claim, client=client)


async def test_connection_revoked_during_preflight_cannot_use_the_send_permit(db, dispatch_case):
    actor, _, claim, _ = dispatch_case
    config = await state.get_config(db, actor.tenant_id, claim.config_id)
    requests = []

    async def handler(request):
        requests.append(request)
        assert request.method == "GET"
        connection = (
            await db.execute(select(Connection).where(Connection.id == config.netsuite_connection_id))
        ).scalar_one()
        connection.status = "revoked"
        await db.flush()
        return httpx.Response(
            200,
            json={
                "success": True,
                "schema_version": 1,
                "account_id": "6738075_SB1",
                "actions_enabled": True,
                "snapshot": claim.before_json,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(mod.NetSuiteActionError, match="guard_connection_unavailable"):
            await mod.dispatch_netsuite_operation(db, actor.tenant_id, claim, client=client)
    assert len(requests) == 1


async def test_approval_deadline_uses_ecmascript_millisecond_utc_date_contract(db, dispatch_case):
    import json
    import re

    actor, proposal, claim, _ = dispatch_case
    requests, handler = transport(dispatch_case)
    async with httpx.AsyncClient(transport=handler) as client:
        await mod.dispatch_netsuite_operation(db, actor.tenant_id, claim, client=client)
    payload = json.loads(next(r.content for r in requests if r.method == "POST"))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", payload["approval_expires_at"])
    from datetime import datetime

    assert datetime.fromisoformat(payload["approval_expires_at"]) <= proposal.valid_until


@pytest.mark.parametrize("near_deadline", [False, True])
async def test_dispatch_has_time_for_preflight_and_write_but_never_extends_operation_deadline(
    db, dispatch_case, monkeypatch, near_deadline
):
    import asyncio
    from datetime import datetime, timedelta, timezone

    actor, _, claim, _ = dispatch_case
    requests, handler = transport(dispatch_case)
    monkeypatch.setattr(mod, "READ_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(mod, "DISPATCH_TIMEOUT_SECONDS", 1, raising=False)
    if near_deadline:
        row = (
            await db.execute(select(TransactionOperation).where(TransactionOperation.id == claim.operation_id))
        ).scalar_one()

        class NearDeadline(datetime):
            @classmethod
            def now(cls, tz=timezone.utc):
                return row.deadline_at - timedelta(seconds=0.04)

        monkeypatch.setattr(mod, "datetime", NearDeadline)

    async def slow_response(request):
        response = await handler.handle_async_request(request)
        if request.method == "POST":
            await asyncio.sleep(0.10)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow_response)) as client:
        result = await mod.dispatch_netsuite_operation(db, actor.tenant_id, claim, client=client)
    assert result["status"] == ("unknown" if near_deadline else "accepted")
    assert result["verified"] is False
    assert len([r for r in requests if r.method == "POST"]) == 1
