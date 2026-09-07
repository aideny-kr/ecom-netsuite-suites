"""Manual source refresh: real tenant authorization and real Redis lease races."""

import importlib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import redis
from sqlalchemy import select

from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.connection import Connection
from app.services.celigo_write_guard import celigo_writes_allowed
from tests.conftest import create_test_user, enable_feature_flag, make_auth_headers

sync = importlib.import_module("app.workers.tasks.celigo_flow_map_sync")
URL = "/api/v1/transaction-ops/setup/refresh-sources"


@pytest.fixture
def lease_tenant():
    tenant = str(uuid4())
    yield tenant
    with redis.Redis.from_url(settings.REDIS_URL) as store:
        keys = list(store.scan_iter(f"celigo-source-refresh:{tenant}:*"))
        if keys:
            store.delete(*keys)


@pytest.fixture
async def refresh_context(db, admin_user, monkeypatch):
    actor, headers = admin_user
    await enable_feature_flag(db, actor.tenant_id, "celigo")
    await enable_feature_flag(db, actor.tenant_id, "reconciliation")
    queued = Mock()
    monkeypatch.setattr(sync.celery_app, "send_task", queued)
    yield actor, headers, queued
    with redis.Redis.from_url(settings.REDIS_URL) as store:
        keys = list(store.scan_iter(f"celigo-source-refresh:{actor.tenant_id}:*"))
        if keys:
            store.delete(*keys)


async def connection(db, tenant_id, *, provider="celigo", status="active"):
    row = Connection(
        tenant_id=tenant_id,
        provider=provider,
        auth_type="token",
        label="Source",
        status=status,
        encrypted_credentials="never-read-or-return-this",
    )
    with celigo_writes_allowed(db):
        db.add(row)
        await db.flush()
    return row


async def test_refresh_queues_only_own_active_celigo_and_audits(client, db, refresh_context, tenant_b):
    actor, headers, queued = refresh_context
    own = await connection(db, actor.tenant_id)
    await connection(db, tenant_b.id)
    await connection(db, actor.tenant_id, provider="netsuite")
    response = await client.post(URL, headers=headers)
    assert response.status_code == 202, response.text
    data = response.json()
    assert data["status"] == "queued"
    assert data["already_running"] is False
    queued.assert_called_once_with(
        "tasks.celigo_flow_map_sync",
        queue="sync",
        task_id=data["request_id"],
        expires=300,
        kwargs={
            "tenant_id": str(actor.tenant_id),
            "connection_id": str(own.id),
            "setup_refresh_id": data["request_id"],
        },
    )
    assert "never-read" not in response.text
    audit = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.tenant_id == actor.tenant_id,
                AuditEvent.action == "transaction_ops.sources.refresh_requested",
            )
        )
    ).scalar_one()
    assert audit.actor_id == actor.id
    again = await client.post(URL, headers=headers)
    assert again.json()["request_id"] == data["request_id"]
    assert again.json()["already_running"] is True
    assert queued.call_count == 1


@pytest.mark.parametrize("flag", ["celigo", "reconciliation"])
async def test_disabled_feature_blocks_post_and_poll(client, db, refresh_context, flag):
    actor, headers, queued = refresh_context
    await enable_feature_flag(db, actor.tenant_id, flag, False)
    assert (await client.post(URL, headers=headers)).status_code == 403
    assert (await client.get(f"{URL}/{uuid4()}", headers=headers)).status_code == 403
    queued.assert_not_called()


async def test_authentication_and_manager_permission_required(client, db, refresh_context, tenant_a):
    _, _, queued = refresh_context
    assert (await client.post(URL)).status_code in (401, 403)
    operator, _ = await create_test_user(db, tenant_a, role_name="source-refresh-unprivileged")
    headers = make_auth_headers(operator)
    assert (await client.post(URL, headers=headers)).status_code == 403
    assert (await client.get(f"{URL}/{uuid4()}", headers=headers)).status_code == 403
    queued.assert_not_called()


@pytest.mark.parametrize("status", ["revoked", "error"])
async def test_unavailable_provider_cannot_queue(client, db, refresh_context, status):
    actor, headers, queued = refresh_context
    await connection(db, actor.tenant_id, status=status)
    response = await client.post(URL, headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "celigo_connection_required"
    queued.assert_not_called()


async def test_multiple_active_connections_require_selection(client, db, refresh_context):
    actor, headers, queued = refresh_context
    await connection(db, actor.tenant_id)
    await connection(db, actor.tenant_id)
    assert (await client.post(URL, headers=headers)).json()["detail"]["code"] == "multiple_celigo_connections"
    queued.assert_not_called()


async def test_status_is_tenant_scoped_and_never_exposes_job_errors(client, db, refresh_context, tenant_b):
    actor, headers, _ = refresh_context
    own = await connection(db, actor.tenant_id)
    request = (await client.post(URL, headers=headers)).json()
    outsider, _ = await create_test_user(db, tenant_b)
    await enable_feature_flag(db, tenant_b.id, "celigo")
    await enable_feature_flag(db, tenant_b.id, "reconciliation")
    url = f"{URL}/{request['request_id']}"
    assert (await client.get(url, headers=make_auth_headers(outsider))).status_code == 404
    assert sync.claim_refresh(str(actor.tenant_id), str(own.id), request["request_id"], reserved=True)
    assert (await client.get(url, headers=headers)).json()["status"] == "running"
    sync.finish_refresh(str(actor.tenant_id), str(own.id), request["request_id"], succeeded=False)
    status = (await client.get(url, headers=headers)).json()
    assert status["status"] == "failed"
    assert status["error_code"] == "refresh_failed"
    assert set(status) == {"request_id", "status", "already_running", "poll_after_seconds", "error_code"}


async def test_queue_failure_cancels_own_reservation_and_can_retry(client, db, refresh_context):
    actor, headers, queued = refresh_context
    own = await connection(db, actor.tenant_id)
    queued.side_effect = RuntimeError("secret broker address")
    response = await client.post(URL, headers=headers)
    assert response.status_code == 503
    assert "secret" not in response.text
    old_request = queued.call_args.kwargs["task_id"]
    assert sync.read_refresh(str(actor.tenant_id), old_request)["status"] == "failed"
    assert not sync.claim_refresh(str(actor.tenant_id), str(own.id), old_request, reserved=True)
    queued.side_effect = None
    retried = await client.post(URL, headers=headers)
    assert retried.status_code == 202
    assert retried.json()["request_id"] != old_request


async def test_redis_failure_fails_closed_before_dispatch(client, db, refresh_context, monkeypatch):
    actor, headers, queued = refresh_context
    await connection(db, actor.tenant_id)
    monkeypatch.setattr(sync, "_refresh_store", Mock(side_effect=redis.ConnectionError("secret")))
    response = await client.post(URL, headers=headers)
    assert response.status_code == 503
    assert "secret" not in response.text
    queued.assert_not_called()


def test_concurrent_reservations_dispatch_once_and_legacy_tasks_cannot_overlap(lease_tenant):
    cid = str(uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        reservations = list(pool.map(lambda _: sync.reserve_refresh(lease_tenant, cid), range(8)))
    assert len({item["request_id"] for item in reservations}) == 1
    assert sum(not item["already_running"] for item in reservations) == 1
    rid = reservations[0]["request_id"]
    assert not sync.claim_refresh(lease_tenant, cid, str(uuid4()), reserved=False)
    assert sync.claim_refresh(lease_tenant, cid, rid, reserved=True)
    assert not sync.claim_refresh(lease_tenant, cid, rid, reserved=True)
    sync.finish_refresh(lease_tenant, cid, str(uuid4()), succeeded=True)
    assert sync.read_refresh(lease_tenant, rid)["status"] == "running"
    sync.finish_refresh(lease_tenant, cid, rid, succeeded=True)
    assert sync.read_refresh(lease_tenant, rid)["status"] == "completed"
    assert sync.claim_refresh(lease_tenant, cid, str(uuid4()), reserved=False)


def test_expired_reservation_reports_failed_and_cannot_run(lease_tenant):
    cid = str(uuid4())
    first = sync.reserve_refresh(lease_tenant, cid)
    with redis.Redis.from_url(settings.REDIS_URL) as store:
        store.delete(sync._lease_key(lease_tenant, cid))
    assert sync.read_refresh(lease_tenant, first["request_id"])["status"] == "failed"
    second = sync.reserve_refresh(lease_tenant, cid)
    assert not sync.claim_refresh(lease_tenant, cid, first["request_id"], reserved=True)
    assert sync.read_refresh(lease_tenant, second["request_id"])["status"] == "queued"


def test_late_dispatch_failure_does_not_cancel_an_already_running_worker(lease_tenant):
    cid = str(uuid4())
    request = sync.reserve_refresh(lease_tenant, cid)
    assert sync.claim_refresh(lease_tenant, cid, request["request_id"], reserved=True)
    sync.cancel_refresh(lease_tenant, cid, request["request_id"])
    assert sync.read_refresh(lease_tenant, request["request_id"])["status"] == "running"
    assert sync.reserve_refresh(lease_tenant, cid)["already_running"] is True


async def test_audit_failure_does_not_leave_a_dispatchable_reservation(client, db, refresh_context, monkeypatch):
    actor, headers, queued = refresh_context
    await connection(db, actor.tenant_id)
    monkeypatch.setattr(
        "app.api.v1.transaction_ops_setup.audit_service.log_event", AsyncMock(side_effect=RuntimeError())
    )
    response = await client.post(URL, headers=headers)
    assert response.status_code == 503
    queued.assert_not_called()


@pytest.mark.parametrize("flag", ["celigo", "reconciliation"])
async def test_worker_rechecks_disabled_setup_flags_before_credentials(db, refresh_context, monkeypatch, flag):
    from contextlib import asynccontextmanager

    actor, _, _ = refresh_context
    own = await connection(db, actor.tenant_id)
    await enable_feature_flag(db, actor.tenant_id, flag, False)

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(sync, "worker_async_session", session)
    credentials = Mock()
    monkeypatch.setattr(sync, "decrypt_credentials", credentials)
    with pytest.raises(sync.CeligoSyncFailedError, match="disabled"):
        await sync._execute(str(actor.tenant_id), str(own.id), require_setup_features=True)
    credentials.assert_not_called()


def test_worker_failure_releases_lease_with_safe_failed_status(lease_tenant, monkeypatch):
    cid = str(uuid4())
    request = sync.reserve_refresh(lease_tenant, cid)
    execute = AsyncMock(side_effect=ValueError("private provider details"))
    monkeypatch.setattr(sync, "_execute", execute)
    with pytest.raises(ValueError, match="private provider"):
        sync.celigo_flow_map_sync.run(lease_tenant, cid, setup_refresh_id=request["request_id"])
    assert sync.read_refresh(lease_tenant, request["request_id"])["status"] == "failed"
    assert sync.reserve_refresh(lease_tenant, cid)["already_running"] is False


def test_worker_completion_and_legacy_call_use_same_lease(lease_tenant, monkeypatch):
    cid = str(uuid4())
    execute = AsyncMock(return_value={"flows_synced": 1})
    monkeypatch.setattr(sync, "_execute", execute)
    request = sync.reserve_refresh(lease_tenant, cid)
    sync.celigo_flow_map_sync.run(lease_tenant, cid, setup_refresh_id=request["request_id"])
    assert sync.read_refresh(lease_tenant, request["request_id"])["status"] == "completed"
    execute.assert_awaited_once_with(lease_tenant, cid, require_setup_features=True)
    sync.celigo_flow_map_sync.run(lease_tenant, cid)
    assert execute.await_count == 2
