"""API contract tests for Agent Lab endpoints (Task 6 — non-SSE).

All endpoints are super-admin-gated.  The ``superadmin_user`` and
``tenant_admin_user`` fixtures are defined here because they don't exist yet in
the global conftest.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.core.config import settings
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers

# ---------------------------------------------------------------------------
# Ensure settings.AGENT_BENCHMARK_TENANT_ID is populated for all tests in
# this module (uses a fixed Framework-like UUID — no real DB row needed for
# list/patterns since both return empty lists on a fresh test DB).
# ---------------------------------------------------------------------------

_TEST_TENANT_ID = "ce3dfaad-626f-4992-84e9-500c8291ca0a"


@pytest.fixture(autouse=True)
def _patch_benchmark_tenant_id():
    original = settings.AGENT_BENCHMARK_TENANT_ID
    settings.AGENT_BENCHMARK_TENANT_ID = _TEST_TENANT_ID
    yield
    settings.AGENT_BENCHMARK_TENANT_ID = original


# ---------------------------------------------------------------------------
# Local fixtures — auth actors
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def tenant_admin_user(db, client):
    """A regular tenant admin — must be 403'd by superadmin-gated endpoints."""
    tenant = await create_test_tenant(db, name="Admin Corp")
    user, _ = await create_test_user(db, tenant, role_name="admin")
    # global_role defaults to "user" — NOT superadmin
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def superadmin_user(db, client):
    """A user with global_role='superadmin'."""
    tenant = await create_test_tenant(db, name="Superadmin Corp")
    user, _ = await create_test_user(db, tenant, role_name="admin")
    user.global_role = "superadmin"
    await db.flush()
    return user, make_auth_headers(user)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_runs_requires_superadmin(client: AsyncClient, tenant_admin_user):
    """Non-super-admin gets 403 on POST /runs."""
    _, headers = tenant_admin_user
    resp = await client.post(
        "/api/v1/agent-lab/runs",
        json={"kind": "benchmark", "mode": "all"},
        headers=headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_runs_creates_row_and_returns_run_id(
    client, superadmin_user, monkeypatch
):
    """Super-admin can POST to create a run; returns run_id."""
    from unittest.mock import MagicMock

    apply_async = MagicMock(return_value=MagicMock(id="task-id"))
    monkeypatch.setattr(
        "app.workers.tasks.agent_lab_runner.agent_lab_run_task.apply_async",
        apply_async,
    )

    _, headers = superadmin_user
    resp = await client.post(
        "/api/v1/agent-lab/runs",
        json={"kind": "benchmark", "mode": "all"},
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "run_id" in body
    assert body["status"] == "running"
    apply_async.assert_called_once()


@pytest.mark.asyncio
async def test_post_runs_returns_409_on_concurrent_same_kind(
    client, superadmin_user, monkeypatch
):
    """Second POST for same kind while first is running returns 409."""
    from unittest.mock import MagicMock

    monkeypatch.setattr(
        "app.workers.tasks.agent_lab_runner.agent_lab_run_task.apply_async",
        MagicMock(),
    )

    _, headers = superadmin_user
    first = await client.post(
        "/api/v1/agent-lab/runs",
        json={"kind": "benchmark", "mode": "all"},
        headers=headers,
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/agent-lab/runs",
        json={"kind": "benchmark", "mode": "all"},
        headers=headers,
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_get_runs_returns_recent(client, superadmin_user):
    _, headers = superadmin_user
    resp = await client.get("/api/v1/agent-lab/runs?days=14", headers=headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_get_patterns_returns_tenant_scoped_list(client, superadmin_user):
    _, headers = superadmin_user
    resp = await client.get("/api/v1/agent-lab/patterns", headers=headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_post_cancel_sets_redis_flag(client, superadmin_user, monkeypatch):
    from unittest.mock import MagicMock

    redis_mock = MagicMock()
    monkeypatch.setattr(
        "app.api.v1.agent_lab.get_sync_redis",
        lambda: redis_mock,
    )
    monkeypatch.setattr(
        "app.workers.tasks.agent_lab_runner.agent_lab_run_task.apply_async",
        MagicMock(),
    )

    _, headers = superadmin_user
    run_resp = await client.post(
        "/api/v1/agent-lab/runs",
        json={"kind": "experiment", "mode": "all"},
        headers=headers,
    )
    run_id = run_resp.json()["run_id"]

    cancel_resp = await client.post(
        f"/api/v1/agent-lab/runs/{run_id}/cancel",
        headers=headers,
    )
    assert cancel_resp.status_code == 200
    redis_mock.set.assert_called_once()
    set_args = redis_mock.set.call_args
    assert "cancel" in set_args[0][0]


@pytest.mark.asyncio
async def test_get_run_snapshot_404_for_unknown(client, superadmin_user):
    _, headers = superadmin_user
    resp = await client.get(
        f"/api/v1/agent-lab/runs/{uuid.uuid4()}",
        headers=superadmin_user[1],
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_runs_returns_400_when_single_mode_missing_case_id(
    client, superadmin_user, monkeypatch
):
    """mode='single' without case_id returns 400, not 500."""
    from unittest.mock import MagicMock

    monkeypatch.setattr(
        "app.workers.tasks.agent_lab_runner.agent_lab_run_task.apply_async",
        MagicMock(),
    )

    _, headers = superadmin_user
    resp = await client.post(
        "/api/v1/agent-lab/runs",
        json={"kind": "benchmark", "mode": "single"},  # no case_id
        headers=headers,
    )
    assert resp.status_code == 400
    assert "case_id" in resp.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_sse_endpoint_streams_events(client, superadmin_user, monkeypatch):
    """SSE endpoint reads from Redis stream and relays events."""
    from unittest.mock import AsyncMock, MagicMock

    # Fake async Redis: first call returns run_started, second returns
    # run_complete so the generator terminates naturally (no infinite loop).
    events_data = [
        [
            (
                b"agent_lab_run:abc",
                [
                    (b"1-0", {b"event": b"run_started", b"data": b'{"total_cases":18}'}),
                    (b"2-0", {b"event": b"run_complete", b"data": b'{"status":"done"}'}),
                ],
            )
        ],
    ]
    iter_events = iter(events_data)

    async def fake_xread(*args, **kwargs):
        return next(iter_events, [])

    fake_async_redis = MagicMock()
    fake_async_redis.xread = fake_xread
    fake_async_redis.aclose = AsyncMock()
    monkeypatch.setattr(
        "app.api.v1.agent_lab.get_async_redis",
        lambda: fake_async_redis,
    )
    monkeypatch.setattr(
        "app.workers.tasks.agent_lab_runner.agent_lab_run_task.apply_async",
        MagicMock(),
    )

    _, headers = superadmin_user
    run_resp = await client.post(
        "/api/v1/agent-lab/runs",
        json={"kind": "benchmark", "mode": "all"},
        headers=headers,
    )
    run_id = run_resp.json()["run_id"]

    async with client.stream(
        "GET",
        f"/api/v1/agent-lab/runs/{run_id}/events",
        headers=headers,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        content = b""
        async for chunk in resp.aiter_bytes():
            content += chunk

    assert b"run_started" in content
    assert b'"total_cases":18' in content
