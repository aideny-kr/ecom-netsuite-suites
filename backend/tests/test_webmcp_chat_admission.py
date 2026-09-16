"""Real Postgres/Redis admission and ownership checks; no provider calls.

Run only in the dedicated WebMCP fixture database (see frontend/WEBMCP.md).
"""

import asyncio
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import chat, chat_runs
from app.core import database
from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.chat import ChatMessage, ChatSession, ChatSubmission
from app.services.chat.run_manager import RunManager

_real_background = chat._run_chat_background


def isolated_fixture_services() -> bool:
    database = make_url(settings.DATABASE_URL_DIRECT or settings.DATABASE_URL)
    redis = make_url(settings.REDIS_URL)
    if database.host not in {"localhost", "127.0.0.1"} or redis.host not in {"localhost", "127.0.0.1"}:
        return False
    local_fixture = database.port == 15435 and database.database == "webmcp" and redis.port == 16381
    ci_fixture = os.getenv("CI") == "true" and settings.APP_ENV == "test" and database.database == "ecom_netsuite_test"
    return local_fixture or ci_fixture


pytestmark = pytest.mark.skipif(
    not isolated_fixture_services(), reason="Requires dedicated WebMCP or CI fixture services"
)


@pytest.fixture
async def fixture_store(monkeypatch):
    # Cross-commit/concurrency tests MUST NOT run against a developer's usual DB.
    if not isolated_fixture_services():
        pytest.skip("Requires dedicated WebMCP or CI fixture services")
    engine = create_async_engine(settings.DATABASE_URL_DIRECT or settings.DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session_factory", factory)
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    session_id = uuid.uuid4()
    async with factory() as db:
        db.add(ChatSession(id=session_id, tenant_id=user.tenant_id, user_id=user.id))
        await db.commit()
    manager = RunManager(settings.REDIS_URL)
    assert manager.available
    background = AsyncMock()
    monkeypatch.setattr(chat, "_run_chat_background", background)
    monkeypatch.setattr(chat, "get_run_manager", lambda: manager)
    monkeypatch.setattr(chat_runs, "get_run_manager", lambda: manager)
    monkeypatch.setattr(chat, "check_chat_burst_limit", lambda *_: True)
    yield factory, manager, user, session_id, background
    async with factory() as db:
        run_ids = (await db.scalars(select(ChatSubmission.run_id).where(ChatSubmission.session_id == session_id))).all()
        await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
        await db.execute(delete(ChatSession).where(ChatSession.id == session_id))
        await db.execute(delete(AuditEvent).where(AuditEvent.tenant_id == user.tenant_id))
        await db.commit()
    for run_id in run_ids:
        keys = list(manager._redis.scan_iter(f"chat:run:{run_id}:*"))
        if keys:
            manager._redis.delete(*keys)
    manager.clear_active_run(str(session_id))
    await engine.dispose()


async def submit(store, body):
    factory, _, user, session_id, _ = store
    async with factory() as db:
        return await chat.send_message(session_id, body, user, db, x_timezone="UTC")


async def test_concurrent_retry_persists_one_message_and_starts_one_worker(fixture_store):
    factory, manager, _, session_id, background = fixture_store
    body = chat.SendMessageRequest(content="fixture only", request_id=uuid.uuid4())
    first, retry = await asyncio.gather(submit(fixture_store, body), submit(fixture_store, body))
    await asyncio.sleep(0)
    assert first["run_id"] == retry["run_id"]
    assert sorted([first["replayed"], retry["replayed"]]) == [False, True]
    background.assert_awaited_once()
    async with factory() as db:
        assert (
            await db.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.session_id == session_id))
            == 1
        )
    # Replay after completion and after loss/expiry of every Redis run key still
    # returns the durable receipt. It NEVER silently launches a second worker.
    keys = list(manager._redis.scan_iter(f"chat:run:{first['run_id']}:*"))
    manager._redis.delete(*keys)
    again = await submit(fixture_store, body)
    assert again["run_id"] == first["run_id"] and again["replayed"]
    background.assert_awaited_once()


async def test_key_reuse_with_changed_input_is_conflict(fixture_store):
    key = uuid.uuid4()
    await submit(fixture_store, chat.SendMessageRequest(content="one", request_id=key))
    with pytest.raises(HTTPException) as exc:
        await submit(fixture_store, chat.SendMessageRequest(content="two", request_id=key))
    assert exc.value.status_code == 409


async def test_cancelling_blocks_new_message_without_persisting_it(fixture_store):
    factory, manager, user, session_id, _ = fixture_store
    receipt = await submit(fixture_store, chat.SendMessageRequest(content="one", request_id=uuid.uuid4()))
    async with factory() as db:
        result = await chat_runs.cancel_run(receipt["run_id"], user, db)
    assert result["status"] == "cancelling"
    assert manager.get_status(receipt["run_id"]) == "cancelling"
    with pytest.raises(HTTPException) as exc:
        await submit(fixture_store, chat.SendMessageRequest(content="two", request_id=uuid.uuid4()))
    assert exc.value.status_code == 409
    async with factory() as db:
        assert (
            await db.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.session_id == session_id))
            == 1
        )


@pytest.mark.parametrize("foreign", ["tenant", "user"])
async def test_actual_database_ownership_blocks_status_stream_cancel(fixture_store, foreign):
    factory, manager, owner, _, _ = fixture_store
    receipt = await submit(fixture_store, chat.SendMessageRequest(content="private", request_id=uuid.uuid4()))
    outsider = SimpleNamespace(id=owner.id, tenant_id=owner.tenant_id)
    setattr(outsider, "tenant_id" if foreign == "tenant" else "id", uuid.uuid4())
    for endpoint in (chat_runs.get_run_status, chat_runs.stream_run, chat_runs.cancel_run):
        async with factory() as db:
            with pytest.raises(HTTPException) as exc:
                await endpoint(receipt["run_id"], outsider, db)
            assert exc.value.status_code == 404
    assert manager.get_status(receipt["run_id"]) == "running"


async def test_key_cannot_approve_a_write(fixture_store):
    with pytest.raises(HTTPException) as exc:
        await submit(
            fixture_store,
            chat.SendMessageRequest(
                content="",
                request_id=uuid.uuid4(),
                write_confirm={"action": "approve", "confirmation_id": "fake"},
            ),
        )
    assert exc.value.status_code == 422


async def test_late_cleanup_does_not_clear_newer_run(fixture_store):
    _, manager, _, session_id, _ = fixture_store
    first = await submit(fixture_store, chat.SendMessageRequest(content="one", request_id=uuid.uuid4()))
    manager.set_status(first["run_id"], "complete")
    second = await submit(fixture_store, chat.SendMessageRequest(content="two", request_id=uuid.uuid4()))
    manager.clear_active_run(str(session_id), first["run_id"])
    assert manager.get_active_run(str(session_id)) == second["run_id"]


async def test_cancel_cannot_overwrite_completed_run(fixture_store):
    _, manager, _, _, _ = fixture_store
    receipt = await submit(fixture_store, chat.SendMessageRequest(content="one", request_id=uuid.uuid4()))
    manager.set_status(receipt["run_id"], "complete")
    assert manager.request_cancel(receipt["run_id"]) is False
    assert manager.get_status(receipt["run_id"]) == "complete"


async def test_receipt_rls_under_non_bypass_table_owner(fixture_store):
    factory, _, user, _, _ = fixture_store
    await submit(fixture_store, chat.SendMessageRequest(content="one", request_id=uuid.uuid4()))
    role = "webmcp_test_" + uuid.uuid4().hex
    async with factory() as db:
        # Role creation and grants roll back with this test transaction.
        assert await db.scalar(
            text("SELECT relforcerowsecurity FROM pg_class WHERE oid = 'chat_submissions'::regclass")
        )
        await db.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
        await db.execute(text(f"GRANT USAGE, CREATE ON SCHEMA public TO {role}"))
        await db.execute(text(f"ALTER TABLE chat_submissions OWNER TO {role}"))
        await db.execute(text(f"SET LOCAL ROLE {role}"))
        await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))
        assert await db.scalar(select(func.count()).select_from(ChatSubmission)) == 1
        await db.execute(text(f"SET LOCAL app.current_tenant_id = '{uuid.uuid4()}'"))
        assert await db.scalar(select(func.count()).select_from(ChatSubmission)) == 0
        with pytest.raises(DBAPIError):
            await db.execute(
                text("""
                INSERT INTO chat_submissions (session_id, request_id, tenant_id, request_hash, run_id)
                VALUES (:session, :request, :tenant, 'hash', :run)
            """),
                {"session": fixture_store[3], "request": uuid.uuid4(), "tenant": user.tenant_id, "run": uuid.uuid4()},
            )
        await db.rollback()


async def test_authenticated_http_roundtrip(client, db, admin_user, monkeypatch):
    """Actual auth, tenant feature check, routes, DB and Redis; provider disabled."""
    manager = RunManager(settings.REDIS_URL)
    assert manager.available
    monkeypatch.setattr(chat, "_run_chat_background", AsyncMock())
    monkeypatch.setattr(chat, "get_run_manager", lambda: manager)
    monkeypatch.setattr(chat_runs, "get_run_manager", lambda: manager)
    user, headers = admin_user
    response = await client.post("/api/v1/chat/sessions", json={"title": "fixture"}, headers=headers)
    assert response.status_code == 201
    session_id = response.json()["id"]
    body = {"content": "Fixture request, no provider", "request_id": str(uuid.uuid4())}
    first = await client.post(f"/api/v1/chat/sessions/{session_id}/messages", json=body, headers=headers)
    assert first.status_code == 202
    run_id = first.json()["run_id"]
    try:
        retry = await client.post(f"/api/v1/chat/sessions/{session_id}/messages", json=body, headers=headers)
        assert retry.status_code == 202 and retry.json()["replayed"] is True
        status = await client.get(f"/api/v1/chat/runs/{run_id}", headers=headers)
        assert status.status_code == 200 and status.json()["status"] == "running"
        assert (await client.get(f"/api/v1/chat/runs/{run_id}")).status_code == 401
        cancellation = await client.post(f"/api/v1/chat/runs/{run_id}/cancel", headers=headers)
        assert cancellation.status_code == 200 and cancellation.json()["status"] == "cancelling"
        manager.set_status(run_id, "cancelled")
        stream = await client.get(f"/api/v1/chat/runs/{run_id}/stream", headers=headers)
        assert stream.status_code == 200 and '"status": "cancelled"' in stream.text
    finally:
        keys = list(manager._redis.scan_iter(f"chat:run:{run_id}:*"))
        manager._redis.delete(*keys)
        manager.clear_active_run(session_id)


@pytest.mark.parametrize(
    "event,outcome",
    [
        ({"type": "text", "content": "done"}, "complete"),
        ({"type": "error", "error": "fixture failure"}, "failed"),
        (
            {"type": "message", "message": {"structured_output": {"type": "write_confirmation"}}},
            "awaiting_confirmation",
        ),
        ({"type": "clarification_required", "data": {}}, "awaiting_clarification"),
    ],
)
async def test_worker_outcome_distinguishes_failure_and_human_attention(fixture_store, monkeypatch, event, outcome):
    factory, manager, user, session_id, _ = fixture_store
    receipt = await submit(fixture_store, chat.SendMessageRequest(content="fixture", request_id=uuid.uuid4()))

    async def deterministic_turn(**kwargs):
        yield event

    monkeypatch.setattr(chat, "run_chat_turn", deterministic_turn)
    async with factory() as db:
        session = await db.get(ChatSession, session_id)
        message = await db.scalar(select(ChatMessage).where(ChatMessage.session_id == session_id))
    await _real_background(
        run_id=receipt["run_id"],
        session_id=str(session_id),
        session=session,
        user_message="fixture",
        user_id=user.id,
        tenant_id=user.tenant_id,
        user_msg=message,
        wizard_step=None,
        user_timezone="UTC",
        agent_id=None,
    )
    assert manager.get_outcome(receipt["run_id"]) == outcome
    assert manager.get_status(receipt["run_id"]) == ("failed" if outcome == "failed" else "complete")
    assert manager.get_active_run(str(session_id)) is None


async def test_retry_receipt_does_not_consume_new_submission_burst_quota(fixture_store, monkeypatch):
    body = chat.SendMessageRequest(content="one logical request", request_id=uuid.uuid4())
    original = await submit(fixture_store, body)
    # The limiter itself is sync and runs in to_thread; count calls without model work.
    from unittest.mock import Mock

    limiter = Mock(return_value=False)
    monkeypatch.setattr(chat, "check_chat_burst_limit", limiter)
    retry = await submit(fixture_store, body)
    assert retry["run_id"] == original["run_id"] and retry["replayed"]
    limiter.assert_not_called()
    with pytest.raises(HTTPException) as denied:
        await submit(fixture_store, chat.SendMessageRequest(content="new work", request_id=uuid.uuid4()))
    assert denied.value.status_code == 429
    limiter.assert_called_once()
