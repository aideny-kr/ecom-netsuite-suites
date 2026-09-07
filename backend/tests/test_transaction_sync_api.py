import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.models.connection import Connection
from app.models.pipeline import CursorState


async def solidus(db, tenant_id, *, status="active", profile="framework_sync"):
    row = Connection(
        tenant_id=tenant_id,
        provider="solidus",
        label="Orders",
        status=status,
        encrypted_credentials="not-exposed",
        metadata_json={"api_profile": profile},
    )
    db.add(row)
    await db.flush()
    return row


async def test_solidus_refresh_dispatches_once_and_returns_queued_status(client, db, admin_user, monkeypatch):
    user, headers = admin_user
    conn = await solidus(db, user.tenant_id)
    send = Mock(return_value=SimpleNamespace(id="unused"))
    monkeypatch.setattr("app.workers.celery_app.celery_app.send_task", send)
    first = await client.post(f"/api/v1/connections/{conn.id}/sync", headers=headers)
    assert first.status_code == 200 and first.json()["status"] == "queued"
    second = await client.post(f"/api/v1/connections/{conn.id}/sync", headers=headers)
    assert second.json()["job_id"] == first.json()["job_id"]
    assert send.call_count == 1
    assert send.call_args.args == ("tasks.solidus_sync",)
    assert send.call_args.kwargs["kwargs"]["tenant_id"] == str(user.tenant_id)
    status = await client.get(f"/api/v1/connections/{conn.id}/sync-status", headers=headers)
    assert status.status_code == 200 and status.json()["status"] == "queued"
    assert "not-exposed" not in status.text


@pytest.mark.parametrize("status", ["error", "revoked", "superseded"])
async def test_refresh_rejects_inactive_source_before_dispatch(client, db, admin_user, monkeypatch, status):
    conn = await solidus(db, admin_user[0].tenant_id, status=status)
    send = Mock()
    monkeypatch.setattr("app.workers.celery_app.celery_app.send_task", send)
    result = await client.post(f"/api/v1/connections/{conn.id}/sync", headers=admin_user[1])
    assert result.status_code == 409
    send.assert_not_called()


async def test_sync_status_and_refresh_are_tenant_scoped(client, db, admin_user, admin_user_b):
    conn = await solidus(db, admin_user_b[0].tenant_id)
    for method, path in (("get", "sync-status"), ("post", "sync")):
        response = await getattr(client, method)(f"/api/v1/connections/{conn.id}/{path}", headers=admin_user[1])
        assert response.status_code == 404


async def test_view_only_user_cannot_trigger_source_reads(client, db, readonly_user):
    conn = await solidus(db, readonly_user[0].tenant_id)
    response = await client.post(f"/api/v1/connections/{conn.id}/sync", headers=readonly_user[1])
    assert response.status_code == 403


async def test_partial_cursor_never_claims_successful_refresh(client, db, admin_user):
    conn = await solidus(db, admin_user[0].tenant_id)
    db.add(
        CursorState(
            connection_id=conn.id,
            object_type="solidus_orders_v1",
            cursor_value=json.dumps({"next_page": 2, "total": 21, "since": "2026-09-01T00:00:00Z"}),
            last_synced_at=datetime.now(timezone.utc),
        )
    )
    await db.flush()
    response = await client.get(f"/api/v1/connections/{conn.id}/sync-status", headers=admin_user[1])
    assert response.status_code == 200
    assert response.json()["status"] == "partial"
    assert response.json()["last_completed_at"] is None
    assert response.json()["source_total"] == 21


async def test_broker_failure_is_visible_and_can_be_retried(client, db, admin_user, monkeypatch):
    conn = await solidus(db, admin_user[0].tenant_id)
    from kombu.exceptions import OperationalError

    send = Mock(side_effect=OperationalError("do not expose broker address or credentials"))
    monkeypatch.setattr("app.workers.celery_app.celery_app.send_task", send)
    response = await client.post(f"/api/v1/connections/{conn.id}/sync", headers=admin_user[1])
    assert response.status_code == 503 and "credentials" not in response.text
    status = await client.get(f"/api/v1/connections/{conn.id}/sync-status", headers=admin_user[1])
    assert status.json()["status"] == "failed"
    send.side_effect = None
    send.return_value = SimpleNamespace(id="unused")
    retry = await client.post(f"/api/v1/connections/{conn.id}/sync", headers=admin_user[1])
    assert retry.status_code == 200 and send.call_count == 2
