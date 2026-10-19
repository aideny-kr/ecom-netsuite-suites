"""Runtime failures must never fall back to host execution or replay create."""

import asyncio
import json
import sys
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import runner_service
from app.services.workspace import command_sandbox as sandbox

IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    requests = []
    state = {"ExitCode": 0, "Running": False, "OOMKilled": False}

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/containers/json"):
            return httpx.Response(200, json=[])
        if "/images/" in request.url.path:
            return httpx.Response(200, json={"Id": IMAGE, "Config": {"Volumes": None}})
        if request.method == "POST":
            return httpx.Response(201, json={"Id": "synthetic"})
        if request.method == "GET":
            return httpx.Response(200, json={"State": state})
        return httpx.Response(204)

    monkeypatch.setattr(sandbox.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handle))
    attach = AsyncMock(return_value=(0, json.dumps({"exit_code": 0, "stdout": "test results", "stderr": ""}), ""))
    monkeypatch.setattr(sandbox, "_attach", attach)
    (tmp_path / "probe.js").write_text("// synthetic project\n")
    args = dict(image=IMAGE, socket="/trusted/docker.sock", docker="/trusted/docker")
    return tmp_path, args, requests, attach, state


@pytest.mark.asyncio
async def test_only_materialized_content_reaches_restricted_container(runtime, monkeypatch):
    root, args, requests, attach, _ = runtime
    monkeypatch.setenv("ENCRYPTION_KEY", "synthetic-host-canary")
    assert await sandbox.run_isolated_command(["npx", "jest"], str(root), 5, **args) == (0, "test results", "")
    spec = json.loads(next(r.content for r in requests if r.method == "POST"))
    host = spec["HostConfig"]
    assert spec["Image"] == IMAGE and spec["User"] == "0:0"
    assert spec["NetworkDisabled"] and host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] and host["Mounts"] == []
    assert host["CapDrop"] == ["ALL"] and host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["CapAdd"] == ["SETUID", "SETGID"]
    assert host["PidsLimit"] == 64 and host["Memory"] > 0 and host["NanoCpus"] > 0
    assert "synthetic-host-canary" not in json.dumps(spec)
    cmd, payload, _ = attach.call_args.args
    assert json.loads(payload) == {"probe.js": "// synthetic project\n"}
    assert cmd[0] == "/trusted/docker" and "unix:///trusted/docker.sock" in cmd
    assert requests[-1].method == "DELETE" and requests[-1].url.params["force"] == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("image", ["", "node:20", "registry/image:latest", "sha256:short"])
async def test_missing_or_mutable_runtime_never_creates_or_launches(runtime, image):
    root, args, requests, attach, _ = runtime
    args["image"] = image
    with pytest.raises(sandbox.CommandSandboxError, match="immutable"):
        await sandbox.run_isolated_command(["npx", "jest"], str(root), 5, **args)
    assert not requests
    attach.assert_not_awaited()


@pytest.mark.asyncio
async def test_links_are_rejected_before_daemon_access(runtime):
    root, args, requests, _, _ = runtime
    (root / "credential-link").symlink_to("/host/private-key")
    with pytest.raises(sandbox.CommandSandboxError, match="links"):
        await sandbox.run_isolated_command(["npx", "jest"], str(root), 5, **args)
    assert not requests


@pytest.mark.asyncio
async def test_ambiguous_create_is_cleaned_without_retry(runtime, monkeypatch):
    root, args, requests, attach, _ = runtime

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/containers/json"):
            return httpx.Response(200, json=[])
        if "/images/" in request.url.path:
            return httpx.Response(200, json={"Id": IMAGE, "Config": {}})
        if request.method == "POST":
            raise httpx.ReadTimeout("synthetic ambiguous response", request=request)
        return httpx.Response(204)

    monkeypatch.setattr(sandbox.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handle))
    with pytest.raises(sandbox.CommandSandboxError, match="unavailable"):
        await sandbox.run_isolated_command(["npx", "jest"], str(root), 5, **args)
    assert [r.method for r in requests] == ["GET", "GET", "POST", "DELETE"]
    assert requests[2].url.params["name"] in requests[3].url.path
    attach.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_removes_container(runtime):
    root, args, requests, attach, _ = runtime
    entered = asyncio.Event()

    async def blocked(*_):
        entered.set()
        await asyncio.Event().wait()

    attach.side_effect = blocked
    task = asyncio.create_task(sandbox.run_isolated_command(["npx", "jest"], str(root), 5, **args))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert requests[-1].method == "DELETE"


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [125, 126, 127])
async def test_runtime_limit_cannot_be_a_passing_check(runtime, exit_code):
    root, args, requests, attach, state = runtime
    state["ExitCode"] = exit_code
    attach.return_value = (exit_code, "partial", "")
    with pytest.raises(sandbox.CommandSandboxError, match="limits"):
        await sandbox.run_isolated_command(["npx", "jest"], str(root), 5, **args)
    assert requests[-1].method == "DELETE"


@pytest.mark.asyncio
async def test_cleanup_failure_cannot_return_success(runtime, monkeypatch):
    root, args, _, _, _ = runtime

    def handle(request):
        if request.url.path.endswith("/containers/json"):
            return httpx.Response(200, json=[])
        if "/images/" in request.url.path:
            return httpx.Response(200, json={"Id": IMAGE, "Config": {}})
        if request.method == "DELETE":
            return httpx.Response(500, text="private daemon diagnostic")
        return httpx.Response(201, json={"State": {"ExitCode": 0}})

    monkeypatch.setattr(sandbox.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handle))
    with pytest.raises(sandbox.CommandSandboxError, match="cleanup failed") as caught:
        await sandbox.run_isolated_command(["npx", "jest"], str(root), 5, **args)
    assert "private daemon diagnostic" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["not-json", "[]", '{"exit_code":1}', '{"exit_code":0,"stdout":[],"stderr":""}'])
async def test_invalid_result_receipt_cannot_pass(runtime, receipt):
    root, args, requests, attach, _ = runtime
    attach.return_value = (0, receipt, "")
    with pytest.raises(sandbox.CommandSandboxError, match="invalid result"):
        await sandbox.run_isolated_command(["npx", "jest"], str(root), 5, **args)
    assert requests[-1].method == "DELETE"


@pytest.mark.asyncio
async def test_only_bounded_json_coverage_returns_to_disposable_snapshot(runtime):
    root, args, _, attach, _ = runtime
    coverage = json.dumps({"total": {"lines": {"pct": 50}}})
    attach.return_value = (
        0,
        json.dumps({"exit_code": 0, "stdout": "", "stderr": "", "coverage_summary": coverage}),
        "",
    )
    await sandbox.run_isolated_command(["npx", "jest"], str(root), 5, **args)
    assert (root / "coverage" / "coverage-summary.json").read_text() == coverage


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_cancel_container_removal(runtime, monkeypatch):
    root, args, _, _, _ = runtime
    deleting = asyncio.Event()
    release = asyncio.Event()

    async def handle(request):
        if request.url.path.endswith("/containers/json"):
            return httpx.Response(200, json=[])
        if "/images/" in request.url.path:
            return httpx.Response(200, json={"Id": IMAGE, "Config": {}})
        if request.method == "DELETE":
            deleting.set()
            await release.wait()
            return httpx.Response(204)
        if request.method == "GET":
            return httpx.Response(200, json={"State": {"ExitCode": 0}})
        return httpx.Response(201)

    monkeypatch.setattr(sandbox.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handle))
    task = asyncio.create_task(sandbox.run_isolated_command(["npx", "jest"], str(root), 5, **args))
    await deleting.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_output_flood_is_bounded_on_host_attachment(monkeypatch):
    monkeypatch.setattr(sandbox, "MAX_WIRE_OUTPUT", 1024)
    with pytest.raises(sandbox.CommandSandboxError, match="output limit"):
        await sandbox._attach([sys.executable, "-c", "print('x'*8192)"], b"", 1)


@pytest.mark.asyncio
async def test_sdk_commands_cannot_enter_credential_free_test_executor(tmp_path):
    with pytest.raises(sandbox.CommandSandboxError, match="separate verified credential executor"):
        await runner_service._run_subprocess(["suitecloud", "project:deploy", "--validate"], str(tmp_path), 5)


@pytest.mark.asyncio
async def test_runner_dispatches_only_registered_test_command(tmp_path, monkeypatch):
    invoke = AsyncMock(return_value=(1, "actual failed check", ""))
    monkeypatch.setattr(sandbox, "run_isolated_command", invoke)
    cmd = runner_service.ALLOWED_COMMANDS["jest_unit_test"]["cmd"]
    assert await runner_service._run_subprocess(cmd, str(tmp_path), 5) == (1, "actual failed check", "")
    assert invoke.call_args.args[0] == [
        "npx",
        "--offline",
        "jest",
        "--runInBand",
        "--json",
        "--coverage",
        "--coverageReporters=json-summary",
    ]


def test_snapshot_preserves_exact_crlf_and_lone_cr_bytes(tmp_path):
    original = b"first\r\nsecond\rthird\n"
    (tmp_path / "fixture.snap").write_bytes(original)
    assert json.loads(sandbox._snapshot(str(tmp_path)))["fixture.snap"].encode() == original


@pytest.mark.asyncio
async def test_input_coverage_cannot_be_reused_as_execution_evidence(runtime):
    root, args, requests, attach, _ = runtime
    (root / "coverage").mkdir()
    (root / "coverage" / "coverage-summary.json").write_text('{"total":{"lines":{"pct":100}}}')
    with pytest.raises(sandbox.CommandSandboxError, match="reserved"):
        await sandbox.run_isolated_command(["npx", "jest"], str(root), 5, **args)
    assert not requests
    attach.assert_not_awaited()


@pytest.mark.asyncio
async def test_coverage_write_failure_does_not_expose_host_path(runtime, monkeypatch):
    root, args, _, attach, _ = runtime
    attach.return_value = (0, json.dumps({"exit_code": 0, "stdout": "", "stderr": "", "coverage_summary": "{}"}), "")
    original = type(root).mkdir

    def fail_coverage(self, *args, **kwargs):
        if self.name == "coverage":
            raise OSError("synthetic private host path")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(root), "mkdir", fail_coverage)
    with pytest.raises(sandbox.CommandSandboxError, match="coverage artifact") as caught:
        await sandbox.run_isolated_command(["npx", "jest"], str(root), 5, **args)
    assert "private host path" not in str(caught.value)


@pytest.mark.asyncio
async def test_supervisor_deadline_preserves_timeout_category(runtime):
    root, args, requests, attach, state = runtime
    state["ExitCode"] = 124
    attach.return_value = (124, "", "")
    with pytest.raises(TimeoutError):
        await sandbox.run_isolated_command(["npx", "jest"], str(root), 5, **args)
    assert requests[-1].method == "DELETE"


@pytest.mark.asyncio
async def test_image_volumes_are_rejected_before_creation(runtime, monkeypatch):
    root, args, requests, attach, _ = runtime

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"Id": IMAGE, "Config": {"Volumes": {"/unbounded": {}}}})

    monkeypatch.setattr(sandbox.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handle))
    with pytest.raises(sandbox.CommandSandboxError, match="persistent volumes"):
        await sandbox.run_isolated_command(["npx", "jest"], str(root), 5, **args)
    assert [r.method for r in requests] == ["GET"]
    attach.assert_not_awaited()
