"""Only expired, inspected, versioned broker leases may be removed."""

import json
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.workspace import command_sandbox as sandbox

IMAGE = "sha256:" + "a" * 64
CID = "b" * 64
LEASE = uuid.uuid4().hex
PREFIX = "suitestudio.workspace-command"


def labels(expires="100"):
    return {
        PREFIX: "1",
        PREFIX + ".protocol": "1",
        PREFIX + ".lease": LEASE,
        PREFIX + ".expires": expires,
        PREFIX + ".scope": "diagnostic",
    }


def inspect(expires="100"):
    return {
        "Id": CID,
        "Name": "/ss-command-" + LEASE,
        "Config": {"Image": IMAGE, "Labels": labels(expires)},
        "State": {"Running": True},
    }


@pytest.fixture
def daemon(monkeypatch):
    state = {"list": [{"Id": CID}], "inspect": inspect(), "deletes": [], "status": 204}

    def handle(r):
        if r.method == "DELETE":
            state["deletes"].append(r.url.path)
            return httpx.Response(state["status"])
        if r.url.path.endswith("/containers/json"):
            assert json.loads(r.url.params["filters"])["label"] == [PREFIX + "=1", PREFIX + ".protocol=1"]
            assert r.url.params["all"] == "1"
            return httpx.Response(200, json=state["list"])
        return httpx.Response(200, json=state["inspect"])

    monkeypatch.setattr(sandbox.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handle))
    return state


@pytest.mark.asyncio
async def test_expired_running_or_stopped_lease_is_removed_by_immutable_id(daemon):
    for running in (True, False):
        daemon["inspect"]["State"]["Running"] = running
        result = await sandbox.reconcile_expired_commands(socket="/trusted/socket", now=100)
        assert result["removed"] == 1
    assert daemon["deletes"] == ["/v1.45/containers/" + CID] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["fresh", "legacy", "name", "lease", "expiry", "id", "image", "scope", "tenant"])
async def test_unexpired_unrecognized_or_unrelated_container_is_never_removed(daemon, change):
    d = daemon["inspect"]
    if change == "fresh":
        d["Config"]["Labels"][PREFIX + ".expires"] = "101"
    elif change == "legacy":
        d["Config"]["Labels"].pop(PREFIX + ".protocol")
    elif change == "name":
        d["Name"] = "/unrelated-service"
    elif change == "lease":
        d["Config"]["Labels"][PREFIX + ".lease"] = "invalid"
    elif change == "expiry":
        d["Config"]["Labels"][PREFIX + ".expires"] = "NaN"
    elif change == "id":
        d["Id"] = "c" * 64
    elif change == "image":
        d["Config"]["Image"] = "node:latest"
    elif change == "scope":
        d["Config"]["Labels"][PREFIX + ".scope"] = "unknown"
    elif change == "tenant":
        d["Config"]["Labels"][PREFIX + ".scope"] = "workspace"
    result = await sandbox.reconcile_expired_commands(socket="/trusted/socket", now=100)
    assert result["removed"] == 0
    assert not daemon["deletes"]


@pytest.mark.asyncio
async def test_delete_failure_is_sanitized_and_not_a_success(daemon):
    daemon["status"] = 500
    with pytest.raises(sandbox.CommandSandboxError, match="reconciliation"):
        await sandbox.reconcile_expired_commands(socket="/trusted/socket", now=100)


@pytest.mark.asyncio
async def test_scan_budget_cannot_silently_skip_residue(daemon):
    daemon["list"] *= 3
    with pytest.raises(sandbox.CommandSandboxError, match="limit"):
        await sandbox.reconcile_expired_commands(socket="/trusted/socket", now=100, limit=2)
    assert not daemon["deletes"]


@pytest.mark.asyncio
async def test_container_disappearing_during_inspection_is_safe(monkeypatch):
    def handle(r):
        if r.url.path.endswith("/containers/json"):
            return httpx.Response(200, json=[{"Id": CID}])
        assert r.method == "GET"
        return httpx.Response(404)

    monkeypatch.setattr(sandbox.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handle))
    assert (await sandbox.reconcile_expired_commands(socket="/trusted/socket", now=100))["removed"] == 0


@pytest.mark.asyncio
async def test_scope_is_task_local_and_restored_after_failure():
    scope = tuple(uuid.uuid4() for _ in range(3))
    assert sandbox.current_command_scope.get() is None
    with pytest.raises(RuntimeError):
        with sandbox.command_run_scope(*scope):
            assert sandbox.current_command_scope.get() == scope
            raise RuntimeError("synthetic")
    assert sandbox.current_command_scope.get() is None


@pytest.mark.asyncio
async def test_scopes_do_not_cross_concurrent_tasks():
    import asyncio

    async def scoped():
        scope = tuple(uuid.uuid4() for _ in range(3))
        with sandbox.command_run_scope(*scope):
            await asyncio.sleep(0)
            labels = sandbox._lease_labels(uuid.uuid4().hex, 5)
            assert labels[PREFIX + ".scope"] == "workspace"
            assert tuple(labels[PREFIX + "." + k] for k in ("run", "tenant", "workspace")) == tuple(map(str, scope))
        assert sandbox.current_command_scope.get() is None

    await asyncio.gather(scoped(), scoped())


@pytest.mark.asyncio
async def test_failed_sweep_prevents_any_new_creation_or_execution(monkeypatch, tmp_path):
    requests = []

    def handle(r):
        requests.append(r)
        if "/images/" in r.url.path:
            return httpx.Response(200, json={"Id": IMAGE, "Config": {}})
        assert r.method == "GET" and r.url.path.endswith("/containers/json")
        return httpx.Response(500, text="private daemon error")

    monkeypatch.setattr(sandbox.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handle))
    attach = AsyncMock()
    monkeypatch.setattr(sandbox, "_attach", attach)
    with pytest.raises(sandbox.CommandSandboxError, match="reconciliation") as caught:
        await sandbox.run_isolated_command(
            ["node"], str(tmp_path), 1, image=IMAGE, socket="/trusted/socket", docker="/trusted/docker"
        )
    assert "private daemon" not in str(caught.value)
    assert all(r.method == "GET" for r in requests)
    attach.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_runner_attaches_existing_run_tenant_workspace_ids(db, tenant_a, admin_user, monkeypatch):
    from app.models.workspace import Workspace
    from app.services import runner_service as runner

    user, _ = admin_user
    ws = Workspace(tenant_id=tenant_a.id, name="Synthetic lease correlation", status="active", created_by=user.id)
    db.add(ws)
    await db.flush()
    run = await runner.create_run(db, tenant_a.id, ws.id, "jest_unit_test", user.id)

    async def command(*args):
        assert sandbox.current_command_scope.get() == (run.id, tenant_a.id, ws.id)
        return 0, "{}", ""

    monkeypatch.setattr(runner, "_run_subprocess", command)
    monkeypatch.setattr(runner, "_materialize_snapshot", AsyncMock(return_value=0))
    assert (await runner.execute_run(db, run.id, tenant_a.id)).status == "passed"
    assert sandbox.current_command_scope.get() is None
