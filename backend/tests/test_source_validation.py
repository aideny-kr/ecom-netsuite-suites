"""Provider-confirmed body reuse; old persisted detail alone is never fresh proof."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.services.http_connector_service import ConnectorReadError, read_json, read_json_response
from app.services.transaction_ops import source_snapshot, source_validation
from app.services.transaction_ops.source_reader import SourceReadError
from tests.test_transaction_source_snapshot import NOW, REF, seed

ETAG = 'W/"saved-order"'


async def cached(db, tenant):
    conn, evidence = await seed(db, tenant)
    evidence["_source_etag"] = ETAG
    assert await source_snapshot.save(db, tenant, conn.id, REF, evidence, now=NOW)
    return conn, evidence


async def test_old_body_is_revalidated_without_download_and_keeps_original_collection_time(db, admin_user):
    tenant = admin_user[0].tenant_id
    conn, evidence = await cached(db, tenant)
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(304, headers={"ETag": ETAG})

    now = datetime.now(timezone.utc)
    assert now - NOW > timedelta(days=1)
    assert await source_snapshot.load(db, tenant, conn.id, REF, since=now - timedelta(days=1), now=now) is None
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await source_validation.read_validated_order(
            db, tenant, None, REF, source_connection_id=conn.id, client=client
        )
    assert len(seen) == 1 and seen[0].headers["if-none-match"] == ETAG
    assert seen[0].url.path == f"/api/sync/orders/{REF}"
    assert result["orders"] == evidence["orders"]
    assert result["source_validation"] == "etag_not_modified"
    assert result["body_collected_at"] == NOW.isoformat()
    assert datetime.fromisoformat(result["read_at"]) >= now
    assert "_validation_connection_fingerprint" not in result
    assert await source_snapshot.save(db, tenant, conn.id, REF, result, now=datetime.now(timezone.utc))
    saved = await source_snapshot.load_for_validation(db, tenant, conn.id, REF, now=datetime.now(timezone.utc))
    assert saved["body_collected_at"] == NOW.isoformat()
    assert saved["read_at"] == result["read_at"]


async def test_changed_resource_replaces_the_body_and_validator(db, admin_user):
    tenant = admin_user[0].tenant_id
    conn, evidence = await cached(db, tenant)
    order = {**evidence["orders"][0], "total": "101", "updated_at": datetime.now(timezone.utc).isoformat()}

    def upstream(request):
        assert request.headers["if-none-match"] == ETAG
        return httpx.Response(200, json=order, headers={"etag": '"new-order"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await source_validation.read_validated_order(
            db, tenant, None, REF, source_connection_id=conn.id, client=client
        )
    assert result["orders"][0]["total"] == "101"
    assert result["_source_etag"] == '"new-order"'
    assert result["read_at"] == result["body_collected_at"]
    assert "source_validation" not in result


@pytest.mark.parametrize("reason", ["missing", "wrong", "no_store", "unauthorized", "not_found", "upstream_error"])
async def test_invalid_or_failed_validation_never_falls_back_to_old_money(db, admin_user, reason):
    tenant = admin_user[0].tenant_id
    conn, _ = await cached(db, tenant)
    status = {"unauthorized": 403, "not_found": 404, "upstream_error": 500}.get(reason, 304)
    headers = {"etag": '"different"' if reason == "wrong" else ETAG}
    if reason == "missing":
        headers = {}
    if reason == "no_store":
        headers["cache-control"] = "no-store"
    transport = httpx.MockTransport(lambda request: httpx.Response(status, headers=headers))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SourceReadError):
            await source_validation.read_validated_order(
                db, tenant, None, REF, source_connection_id=conn.id, client=client
            )
    saved = await source_snapshot.load_for_validation(db, tenant, conn.id, REF, now=datetime.now(timezone.utc))
    assert saved["read_at"] == NOW.isoformat()


async def test_other_tenant_cannot_send_stored_validator_or_use_cached_body(db, admin_user):
    conn, _ = await cached(db, admin_user[0].tenant_id)
    upstream = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(SourceReadError):
            await source_validation.read_validated_order(
                db, uuid4(), None, REF, source_connection_id=conn.id, client=client
            )
    upstream.assert_not_awaited()


async def test_credentials_rotated_between_lookup_and_request_do_not_send_old_validator(db, admin_user, monkeypatch):
    tenant = admin_user[0].tenant_id
    conn, evidence = await cached(db, tenant)
    pending = await source_snapshot.load_for_validation(db, tenant, conn.id, REF, now=datetime.now(timezone.utc))
    pending["_validation_connection_fingerprint"] = "old-partition"
    monkeypatch.setattr(source_snapshot, "load_for_validation", AsyncMock(return_value=pending))

    def upstream(request):
        assert "if-none-match" not in request.headers
        return httpx.Response(200, json=evidence["orders"][0], headers={"etag": ETAG})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await source_validation.read_validated_order(
            db, tenant, None, REF, source_connection_id=conn.id, client=client
        )
    assert "source_validation" not in result


@pytest.mark.parametrize("etag", ["*", '"one", "two"', 'W/"valid"\r\nX-Header: bad', '"' + "x" * 513 + '"'])
async def test_validator_header_injection_or_wildcards_never_reach_http(etag):
    upstream = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(ConnectorReadError, match="invalid_validator"):
            await read_json_response(
                {"base_url": "https://shop.example/api/", "auth_type": "none"},
                "orders/1",
                if_none_match=etag,
                client=client,
            )
    upstream.assert_not_awaited()


async def test_existing_nonconditional_read_rejects_unsolicited_304():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(304, headers={"etag": ETAG}))
    ) as client:
        with pytest.raises(ConnectorReadError, match="http_error"):
            await read_json({"base_url": "https://shop.example/api/", "auth_type": "none"}, "orders/1", client=client)


async def test_daily_runner_uses_provider_validation_and_preserves_provenance(db, admin_user, monkeypatch):
    from app.services.transaction_ops.runner import run_investigation
    from tests.test_transaction_ops_runner import State, missing_target

    tenant = admin_user[0].tenant_id
    conn, _ = await cached(db, tenant)
    now = datetime.now(timezone.utc)
    state = State(window=True)
    state.tenant = tenant
    state.run.created_at = now - timedelta(seconds=1)
    state.run.deadline_at = now + timedelta(minutes=15)
    state.run.config_snapshot.update(source_step_id=None, source_connection_id=str(conn.id))
    state.run.progress_json = {
        "scan_complete": True,
        "refund_scan_complete": True,
        "destination_scan_complete": True,
        "dependency_scan_complete": True,
        "pending_refs": [REF],
    }
    validate = AsyncMock(return_value=(None, ETAG, True))
    monkeypatch.setattr(source_validation, "read_json_response", validate)
    target = missing_target()
    target["observed_at"] = now.isoformat()
    result = await run_investigation(
        db,
        tenant,
        state.run_id,
        _state=state,
        _clock=lambda: datetime.now(timezone.utc),
        _enabled=AsyncMock(return_value=True),
        _target_reader=AsyncMock(return_value=target),
        _order_mirror=AsyncMock(),
    )
    assert result["termination_reason"] == "done"
    validate.assert_awaited_once()
    assert validate.call_args.kwargs["if_none_match"] == ETAG
    assert state.run.progress_json["source_body_validations"] == 1
    assert not state.run.progress_json.get("source_detail_reads")
    report = state.reports[REF]
    assert report["source_provenance"]["source_validation"] == "etag_not_modified"
    assert report["source_provenance"]["body_collected_at"] == NOW.isoformat()
    assert datetime.fromisoformat(report["source"]["observed_at"]) >= now
    assert "_source_etag" not in str(report)


async def test_exact_order_investigation_uses_fresh_reader_without_conditional_cache(monkeypatch):
    from app.services.transaction_ops import source_reader
    from app.services.transaction_ops.runner import run_investigation
    from tests.test_transaction_ops_runner import State, missing_target, source_order

    state = State()
    state.run.created_at = NOW
    state.run.config_snapshot.update(source_step_id=None, source_connection_id=str(uuid4()))
    fresh = AsyncMock(return_value=source_order())
    conditional = AsyncMock(side_effect=AssertionError("Exact investigations must fetch fresh detail"))
    monkeypatch.setattr(source_reader, "read_framework_order", fresh)
    monkeypatch.setattr(source_validation, "read_validated_order", conditional)
    result = await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _clock=lambda: NOW,
        _enabled=AsyncMock(return_value=True),
        _target_reader=AsyncMock(return_value=missing_target()),
        _order_mirror=AsyncMock(),
    )
    assert result["termination_reason"] == "done"
    fresh.assert_awaited_once()
    conditional.assert_not_awaited()
