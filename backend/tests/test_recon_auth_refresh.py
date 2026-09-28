"""Native read auth recovers once without stale credentials or unmetered retries."""

import asyncio
import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.services import netsuite_oauth_service as oauth
from app.services import oauth_refresh_lock as locks
from app.services.transaction_ops import netsuite_reader as native
from app.services.transaction_ops.auth_recovery import rejected_read_scope
from app.services.transaction_ops.read_recovery import ReadBudgetExhaustedError, read_with_recovery
from tests.test_netsuite_bulk_concurrency import auth as auth_fixture
from tests.test_transaction_ops_netsuite_reader import ACCOUNT, CONNECTION, TENANT


@pytest.fixture
def auth(monkeypatch):
    return auth_fixture.__wrapped__(monkeypatch)


@pytest.fixture
def credentials(monkeypatch):
    value = dict(
        access_token="old",
        refresh_token="refresh",
        client_id="client",
        account_id="12345",
        expires_at=time.time() + 120,
    )
    connection = SimpleNamespace(id="connection", status="active", encrypted_credentials="encrypted")
    db = AsyncMock()
    monkeypatch.setattr(oauth, "decrypt_credentials", lambda _: dict(value))
    monkeypatch.setattr(oauth, "encrypt_credentials", lambda new: value.update(new) or "rotated")
    monkeypatch.setattr(locks, "acquire", Mock(return_value="owner"))
    monkeypatch.setattr(locks, "release", Mock())
    monkeypatch.setattr(
        oauth,
        "refresh_tokens_with_client",
        AsyncMock(return_value=dict(access_token="new", refresh_token="new-refresh", expires_in=3600)),
    )
    return db, connection, value


async def test_native_margin_refreshes_before_long_read(credentials):
    db, connection, value = credentials
    assert await oauth.get_valid_token(db, connection, min_validity_seconds=300) == "new"
    oauth.refresh_tokens_with_client.assert_awaited_once_with("12345", "refresh", "client")
    assert value["issued_at"] < value["expires_at"]
    db.commit.assert_awaited_once()
    locks.release.assert_called_once_with("oauth_refresh:connection", "owner")


@pytest.mark.parametrize("fresh", [False, True])
async def test_lock_waiter_returns_only_sufficiently_valid_token(credentials, monkeypatch, fresh):
    db, connection, value = credentials
    value["expires_at"] = time.time() - 1
    locks.acquire.return_value = None

    async def reload(*args):
        if fresh:
            value.update(access_token="other-worker", expires_at=time.time() + 3600)

    db.refresh.side_effect = reload
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    assert await oauth.get_valid_token(db, connection, min_validity_seconds=300) == ("other-worker" if fresh else None)
    oauth.refresh_tokens_with_client.assert_not_awaited()
    assert db.refresh.await_count == (1 if fresh else 5)


@pytest.mark.parametrize("changed", [False, True])
async def test_rejected_token_rotates_only_if_still_current(credentials, changed):
    db, connection, value = credentials
    value.update(access_token="new" if changed else "old", expires_at=time.time() + 3600)
    assert (
        await oauth.get_valid_token(
            db, connection, min_validity_seconds=300, rejected_token_sha=hashlib.sha256(b"old").hexdigest()
        )
        == "new"
    )
    assert oauth.refresh_tokens_with_client.await_count == int(not changed)


async def test_cancellation_waits_for_rotated_credential_commit_before_unlock(credentials):
    db, connection, _ = credentials
    committing, finish = asyncio.Event(), asyncio.Event()

    async def commit():
        committing.set()
        await finish.wait()

    db.commit.side_effect = commit
    task = asyncio.create_task(oauth.get_valid_token(db, connection, min_validity_seconds=300))
    await committing.wait()
    task.cancel()
    await asyncio.sleep(0)
    locks.release.assert_not_called()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    locks.release.assert_called_once()


def test_refresh_lock_failure_is_closed_and_release_checks_owner(monkeypatch):
    client = Mock()
    client.set.return_value = True
    monkeypatch.setattr(locks, "_get_redis", lambda: client)
    owner = locks.acquire("lock")
    assert isinstance(owner, str) and len(owner) == 48
    locks.release("lock", owner)
    assert "== ARGV[1]" in client.eval.call_args.args[0]
    assert client.eval.call_args.args[1:] == (1, "lock", owner)
    client.set.side_effect = ConnectionError()
    assert locks.acquire("lock") is None


@pytest.mark.parametrize("second_status", [200, 401, 403])
async def test_real_reader_retries_only_one_401_with_new_auth_and_reserved_spend(auth, monkeypatch, second_status):
    db, _ = auth
    headers = []

    async def token(*args, **kwargs):
        assert kwargs["min_validity_seconds"] == 300
        if kwargs.get("rejected_token_sha"):
            assert kwargs["rejected_token_sha"] == hashlib.sha256(b"old").hexdigest()
            return "new"
        return "old"

    monkeypatch.setattr(native, "get_valid_token", AsyncMock(side_effect=token))

    def wire(request):
        headers.append(request.headers["authorization"])
        return httpx.Response(401 if len(headers) == 1 else second_status, json={})

    reserve, save = AsyncMock(return_value=True), AsyncMock()
    progress = {"processed": 1801, "last_source_id": 42}
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:

        async def factory():
            async with native.authenticated_reader(
                db, TENANT, CONNECTION, ACCOUNT, client=client, max_api_calls=1
            ) as reader:
                return await reader.request("GET", "/record/v1/salesOrder/100")

        call = read_with_recovery(
            factory, retry_calls=2, progress=progress, reserve=reserve, save=save, remaining=lambda: 170
        )
        if second_status == 200:
            assert await call == {}
        else:
            with pytest.raises(native.NetSuiteEvidenceError, match=str(second_status)):
                await call
    assert headers == ["Bearer old", "Bearer new"]
    reserve.assert_awaited_once_with(2)
    assert progress["auth_read_retry_count"] == 1 and progress["processed"] == 1801 and progress["last_source_id"] == 42
    assert rejected_read_scope.get() is None
    assert "new" not in repr(progress) and "Bearer" not in repr(progress)


async def test_auth_retry_cannot_send_without_budget(auth, monkeypatch):
    db, _ = auth
    wire = Mock(return_value=httpx.Response(401, json={}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:

        async def factory():
            async with native.authenticated_reader(db, TENANT, CONNECTION, ACCOUNT, client=client) as reader:
                return await reader.request("GET", "/record/v1/salesOrder/100")

        with pytest.raises(ReadBudgetExhaustedError):
            await read_with_recovery(
                factory,
                retry_calls=2,
                progress={},
                reserve=AsyncMock(return_value=False),
                save=AsyncMock(),
                remaining=lambda: 170,
            )
    assert wire.call_count == 1


def test_real_redis_expired_owner_cannot_unlock_successor():
    from urllib.parse import urlsplit
    from uuid import uuid4

    import redis

    from app.core.config import settings

    assert urlsplit(settings.REDIS_URL).hostname in {"localhost", "127.0.0.1", "redis"}
    client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    key = "test-oauth-owner:" + uuid4().hex
    try:
        owner = locks.acquire(key)
        assert owner and locks.acquire(key) is None
        client.set(key, "successor", ex=30)
        locks.release(key, owner)
        assert client.get(key) == "successor"
        locks.release(key, "successor")
        assert client.get(key) is None
    finally:
        client.delete(key)
        client.close()


@pytest.mark.parametrize("change", ["rotated", "revoked", "account"])
def test_proactive_rechecks_credential_under_owned_lock(monkeypatch, change):
    from datetime import datetime, timezone

    from app.core import encryption
    from app.workers.tasks import proactive_token_refresh as proactive

    old = dict(
        auth_type="oauth2",
        account_id="12345",
        client_id="client",
        access_token="old",
        refresh_token="old-refresh",
        expires_at=time.time() - 1,
    )
    fresh = {**old, "access_token": "new", "refresh_token": "new-refresh", "expires_at": time.time() + 3600}
    if change == "account":
        fresh["account_id"] = "different"
    record = SimpleNamespace(id="connection", status="active", encrypted_credentials="old")
    db = Mock()

    def reload(_):
        record.encrypted_credentials = "new"
        if change == "revoked":
            record.status = "revoked"

    db.refresh.side_effect = reload
    monkeypatch.setattr(encryption, "decrypt_credentials", lambda value: old if value == "old" else fresh)
    monkeypatch.setattr(locks, "acquire", Mock(return_value="owner"))
    monkeypatch.setattr(locks, "release", Mock())
    monkeypatch.setattr(proactive, "_run_async_refresh", Mock())
    stats = dict(checked=0, refreshed=0, errors=0, skipped_locked=0)
    proactive._refresh_single(db, record, "oauth_refresh", stats, datetime.now(timezone.utc), None)
    proactive._run_async_refresh.assert_not_called()
    db.commit.assert_not_called()
    locks.release.assert_called_once_with("oauth_refresh:connection", "owner")


@pytest.mark.parametrize("change", ["revoked", "account", "client"])
async def test_reactive_refresh_rechecks_identity_under_lock(credentials, change):
    db, connection, value = credentials

    async def reload(_):
        if change == "revoked":
            connection.status = "revoked"
        else:
            value["account_id" if change == "account" else "client_id"] = "different"

    db.refresh.side_effect = reload
    assert await oauth.get_valid_token(db, connection, min_validity_seconds=300) is None
    oauth.refresh_tokens_with_client.assert_not_awaited()


async def test_first_403_never_renews_credentials_or_retries(auth):
    db, _ = auth
    wire = Mock(return_value=httpx.Response(403, json={}))
    reserve = AsyncMock(return_value=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:

        async def factory():
            async with native.authenticated_reader(db, TENANT, CONNECTION, ACCOUNT, client=client) as reader:
                return await reader.request("GET", "/record/v1/salesOrder/100")

        with pytest.raises(native.NetSuiteEvidenceError, match="403"):
            await read_with_recovery(
                factory, retry_calls=2, progress={}, reserve=reserve, save=AsyncMock(), remaining=lambda: 170
            )
    assert wire.call_count == 1
    reserve.assert_not_awaited()
