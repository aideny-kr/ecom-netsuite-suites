"""No provider mutations: every request is handled by an in-memory transport."""

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.schemas.transaction_runs import ClaimedOperation
from app.services.transaction_ops import celigo_actions as mod

FLOW, IMPORT, EXPORT, DEST, SCRIPT = [str(x) * 24 for x in range(1, 6)]
REF, ERROR, RETRY = "R123456789-EU", "error-one", "snapshot-one"


@pytest.fixture
def env(monkeypatch):
    tenant, step_id, connection_id, flow_pk = uuid4(), uuid4(), uuid4(), uuid4()
    step = SimpleNamespace(
        id=step_id,
        tenant_id=tenant,
        celigo_connection_id=connection_id,
        flow_id=flow_pk,
        celigo_id=IMPORT,
        role="processor",
        branch_id=None,
        record_type="salesorder",
        operation="add",
        connection_celigo_id=DEST,
    )
    flow = SimpleNamespace(
        id=flow_pk, tenant_id=tenant, celigo_connection_id=connection_id, celigo_id=FLOW, disabled=False
    )
    connection = SimpleNamespace(
        id=connection_id,
        tenant_id=tenant,
        provider="celigo",
        status="active",
        encrypted_credentials="encrypted",
        metadata_json={"region": "us"},
    )
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(one_or_none=lambda: (step, flow, connection))
    monkeypatch.setattr(mod, "set_tenant_context", AsyncMock())
    monkeypatch.setattr(mod, "decrypt_credentials", lambda _: {"token": "private-token"})
    docs = {
        f"/v1/flows/{FLOW}": {
            "_id": FLOW,
            "disabled": False,
            "pageGenerators": [{"_exportId": EXPORT}],
            "pageProcessors": [{"_importId": IMPORT}],
        },
        f"/v1/imports/{IMPORT}": {
            "_id": IMPORT,
            "_connectionId": DEST,
            "adaptorType": "NetSuiteDistributedImport",
            "netsuite_da": {
                "recordType": "salesorder",
                "operation": "add",
                "mapping": {"fields": [{"generate": "subsidiary", "hardCodedValue": "1"}]},
            },
            "hooks": {"preMap": {"_scriptId": SCRIPT, "function": "preMap"}},
        },
        f"/v1/exports/{EXPORT}": {"_id": EXPORT, "_connectionId": "6" * 24, "http": {"method": "GET"}},
        f"/v1/connections/{DEST}": {
            "_id": DEST,
            "type": "netsuite",
            "offline": False,
            "netsuite": {"account": "6738075", "tokenId": "never-return-token"},
        },
        f"/v1/scripts/{SCRIPT}": {"_id": SCRIPT, "content": "function preMap(o) { return o.data; }"},
        f"/v1/flows/{FLOW}/{IMPORT}/errors": {
            "errors": [
                {
                    "errorId": ERROR,
                    "retryDataKey": RETRY,
                    "traceKey": REF,
                    "code": "DUP_RCRD",
                    "source": "application",
                    "occurredAt": "2026-09-04T00:00:00Z",
                    "message": "private customer@example.com",
                }
            ]
        },
        f"/v1/flows/{FLOW}/{IMPORT}/{RETRY}/data": {
            "retryDataKey": RETRY,
            "stage": "page_processor_import",
            "pgExportId": EXPORT,
            "oneToMany": False,
            "data": {"number": REF, "email": "customer@example.com"},
        },
    }
    requests, writes = [], []

    def handler(request):
        requests.append(request)
        if request.method != "GET":
            writes.append(request)
            return httpx.Response(204)
        return httpx.Response(200, json=deepcopy(docs[request.url.path]))

    return SimpleNamespace(
        tenant=tenant,
        step=step,
        flow=flow,
        connection=connection,
        db=db,
        docs=docs,
        requests=requests,
        writes=writes,
        handler=handler,
    )


async def read(env, **kwargs):
    async with httpx.AsyncClient(transport=httpx.MockTransport(env.handler)) as client:
        return await mod.read_celigo_error_evidence(
            env.db, env.tenant, env.step.id, REF, error_id=ERROR, client=client, **kwargs
        )


async def test_exact_live_error_has_complete_scoped_evidence_without_private_payload(env):
    evidence = await read(env)
    assert evidence["complete"] is True and evidence["scope"]["account_id"] == "6738075"
    assert evidence["scope"]["subsidiary_id"] == "1"
    assert evidence["error"]["error_id"] == ERROR
    assert evidence["order_reference"] == REF and len(evidence["config_fingerprint"]) == 64
    assert len(evidence["fingerprint"]) == 64 and not env.writes
    assert "private-token" not in json.dumps(evidence) and "customer@example.com" not in json.dumps(evidence)
    assert "never-return-token" not in json.dumps(evidence)
    assert all(r.headers["authorization"] == "Bearer private-token" for r in env.requests)
    assert len(env.requests) <= mod.MAX_READ_CALLS


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda e: setattr(e.connection, "tenant_id", uuid4()), "scope_unavailable"),
        (lambda e: setattr(e.connection, "status", "revoked"), "scope_unavailable"),
        (lambda e: setattr(e.step, "role", "generator"), "unsupported_target"),
        (lambda e: e.docs[f"/v1/flows/{FLOW}"].update(disabled=True), "inactive_flow"),
        (lambda e: e.docs[f"/v1/flows/{FLOW}"].update(pageProcessors=[]), "target_not_in_flow"),
        (lambda e: e.docs[f"/v1/imports/{IMPORT}"]["netsuite_da"].update(recordType="customer"), "unsupported_target"),
        (lambda e: e.docs[f"/v1/connections/{DEST}"].update(offline=True), "inactive_destination"),
        (
            lambda e: e.docs[f"/v1/flows/{FLOW}/{IMPORT}/{RETRY}/data"]["data"].update(number="R123456789"),
            "error_reference_unproven",
        ),
        (
            lambda e: e.docs[f"/v1/flows/{FLOW}/{IMPORT}/{RETRY}/data"].update(stage="page_generator"),
            "unsupported_retry_stage",
        ),
        (lambda e: e.docs[f"/v1/flows/{FLOW}/{IMPORT}/{RETRY}/data"].update(oneToMany=True), "unsupported_retry_shape"),
    ],
)
async def test_scope_or_reference_conflicts_fail_closed(env, mutate, code):
    mutate(env)
    with pytest.raises(mod.CeligoActionError, match=code):
        await read(env)
    assert not env.writes


async def test_message_or_trace_key_cannot_replace_full_retry_record_reference(env):
    env.docs[f"/v1/flows/{FLOW}/{IMPORT}/{RETRY}/data"]["data"] = {"unrelated": REF}
    with pytest.raises(mod.CeligoActionError, match="error_reference_unproven"):
        await read(env)


async def test_attached_script_change_changes_provider_fingerprint(env):
    before = await read(env)
    env.docs[f"/v1/scripts/{SCRIPT}"]["content"] += " // changed"
    after = await read(env)
    assert before["config_fingerprint"] != after["config_fingerprint"]
    assert before["fingerprint"] != after["fingerprint"]


@pytest.mark.parametrize(
    "url",
    ["https://evil.invalid/errors", f"https://api.integrator.io/v1/flows/{FLOW}/other/errors", "//evil.invalid/errors"],
)
async def test_pagination_cannot_leave_exact_error_path(env, url):
    env.docs[f"/v1/flows/{FLOW}/{IMPORT}/errors"] = {"errors": [], "nextPageURL": url}
    with pytest.raises(mod.CeligoActionError, match="invalid_pagination"):
        await read(env)
    assert all(r.url.host == "api.integrator.io" for r in env.requests)


async def test_missing_exact_error_is_incomplete_not_an_empty_success(env):
    env.docs[f"/v1/flows/{FLOW}/{IMPORT}/errors"] = {"errors": []}
    evidence = await read(env)
    assert evidence["complete"] is False and evidence["error"] is None


async def prepare_dispatch(env, monkeypatch):
    evidence = await read(env)
    now = datetime.now(timezone.utc)
    claim = ClaimedOperation(
        operation_id=uuid4(),
        proposal_id=uuid4(),
        work_key="a" * 64,
        config_id=uuid4(),
        action="resolve_celigo_error",
        currency="EUR",
        netsuite_account_id="6738075",
        subsidiary_id="1",
        record_type="salesorder",
        target_record_id="91",
        before_json={"celigo_error_id": ERROR, "celigo_error_state": "open"},
        after_json={"celigo_error_id": ERROR, "celigo_error_state": "resolved"},
    )
    proposal = SimpleNamespace(
        id=claim.proposal_id,
        tenant_id=env.tenant,
        config_id=claim.config_id,
        order_reference=REF,
        status="approved",
        valid_until=now + timedelta(minutes=10),
        evidence_json={"celigo": evidence},
    )
    config = SimpleNamespace(id=claim.config_id, tenant_id=env.tenant, enabled=True, target_step_id=env.step.id)
    monkeypatch.setattr(mod.state_service, "get_proposal", AsyncMock(return_value=proposal))
    monkeypatch.setattr(mod.state_service, "get_config", AsyncMock(return_value=config))
    reservation = AsyncMock(return_value=True)
    monkeypatch.setattr(mod.state_service, "reserve_operation_dispatch", reservation, raising=False)
    env.requests.clear()
    return claim, evidence, reservation, proposal


async def dispatch(env, claim, evidence):
    async with httpx.AsyncClient(transport=httpx.MockTransport(env.handler)) as client:
        return await mod.dispatch_celigo_resolution(env.db, env.tenant, claim, evidence, client=client)


async def test_resolution_reserves_after_revalidation_before_one_exact_mutation(env, monkeypatch):
    claim, evidence, reserve, _ = await prepare_dispatch(env, monkeypatch)

    async def authorize(*args, **kwargs):
        assert env.requests and not env.writes
        assert kwargs["provider"] == "celigo" and len(kwargs["payload_fingerprint"]) == 64
        return True

    reserve.side_effect = authorize
    receipt = await dispatch(env, claim, evidence)
    assert len(env.writes) == 1
    request = env.writes[0]
    assert request.method == "PUT" and request.url.path == f"/v1/flows/{FLOW}/{IMPORT}/resolved"
    assert json.loads(request.content) == {"errors": [ERROR]}
    assert receipt["status"] == "accepted" and receipt["verified"] is False
    env.db.commit.assert_not_awaited()


async def test_already_reserved_dispatch_never_sends(env, monkeypatch):
    claim, evidence, reserve, _ = await prepare_dispatch(env, monkeypatch)
    reserve.return_value = False
    with pytest.raises(mod.CeligoActionError, match="dispatch_already_reserved"):
        await dispatch(env, claim, evidence)
    assert not env.writes


async def test_changed_live_script_prevents_reservation_or_write(env, monkeypatch):
    claim, evidence, reserve, _ = await prepare_dispatch(env, monkeypatch)
    env.docs[f"/v1/scripts/{SCRIPT}"]["content"] += " // changed"
    with pytest.raises(mod.CeligoActionError, match="provider_evidence_changed"):
        await dispatch(env, claim, evidence)
    reserve.assert_not_awaited()
    assert not env.writes


@pytest.mark.parametrize(
    "change",
    [
        {"action": "sync_missing_order"},
        {"target_record_id": None},
        {"after_json": {"celigo_error_id": ERROR, "celigo_error_state": "resolved", "total": "12"}},
        {"after_json": {"celigo_error_id": "other", "celigo_error_state": "resolved"}},
        {"subsidiary_id": "2"},
        {"netsuite_account_id": "99999"},
    ],
)
async def test_unapproved_scope_or_extra_effect_cannot_reach_provider_mutation(env, monkeypatch, change):
    claim, evidence, reserve, _ = await prepare_dispatch(env, monkeypatch)
    with pytest.raises(mod.CeligoActionError):
        await dispatch(env, claim.model_copy(update=change), evidence)
    reserve.assert_not_awaited()
    assert not env.writes


async def test_network_failure_after_reservation_is_unknown_without_retry(env, monkeypatch):
    claim, evidence, reserve, _ = await prepare_dispatch(env, monkeypatch)
    original = env.handler

    def handler(request):
        if request.method == "PUT":
            env.writes.append(request)
            raise httpx.ReadTimeout("private provider response")
        return original(request)

    env.handler = handler
    receipt = await dispatch(env, claim, evidence)
    assert receipt["status"] == "unknown" and "private" not in json.dumps(receipt)
    assert len(env.writes) == 1 and reserve.await_count == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("code", {"private": "customer@example.com"}),
        ("source", ["customer@example.com"]),
        ("occurredAt", {"private": "customer@example.com"}),
    ],
)
async def test_error_metadata_never_exports_an_unvalidated_nested_payload(env, field, value):
    env.docs[f"/v1/flows/{FLOW}/{IMPORT}/errors"]["errors"][0][field] = value
    with pytest.raises(mod.CeligoActionError, match="invalid_error_metadata"):
        await read(env)


@pytest.mark.parametrize("key", [".", "..", "../other", "a/b", "a\\b"])
async def test_retry_keys_cannot_change_the_resource_path(env, key):
    env.docs[f"/v1/flows/{FLOW}/{IMPORT}/errors"]["errors"][0]["retryDataKey"] = key
    with pytest.raises(mod.CeligoActionError, match="invalid_error_identity"):
        await read(env)


async def test_conflicting_netsuite_account_alias_blocks_scope(env):
    env.docs[f"/v1/connections/{DEST}"]["netsuite"]["accountId"] = "999999"
    with pytest.raises(mod.CeligoActionError, match="destination_account_unproven"):
        await read(env)


@pytest.mark.parametrize("body", [b'{"x": NaN}', b'{"x":1,"x":2}', b'{"x":1e999999999999999999999}'])
async def test_malformed_json_financial_values_fail_safely(env, body):
    env.handler = lambda request: httpx.Response(200, content=body)
    with pytest.raises(mod.CeligoActionError):
        await read(env)


async def test_dynamic_subsidiary_is_unknown_and_cannot_authorize_resolution(env, monkeypatch):
    env.docs[f"/v1/imports/{IMPORT}"]["netsuite_da"]["mapping"]["fields"] = [
        {"generate": "subsidiary", "extract": "business_entity", "lookupName": "subsidiary_map"}
    ]
    claim, evidence, reserve, _ = await prepare_dispatch(env, monkeypatch)
    assert evidence["scope"]["subsidiary_id"] is None
    with pytest.raises(mod.CeligoActionError, match="resolution_scope_unproven"):
        await dispatch(env, claim, evidence)
    reserve.assert_not_awaited()
