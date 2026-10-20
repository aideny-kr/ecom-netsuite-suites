"""Synthetic broker tests; no target authorization or real certificate handshake."""

import asyncio
import json
from dataclasses import asdict
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.workspace import command_sandbox as broker
from app.services.workspace import credential_executor as executor

IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def runtime(monkeypatch):
    requests = []
    state = {"ExitCode": 0, "Running": False, "OOMKilled": False}

    def handle(request):
        requests.append(request)
        if "/images/" in request.url.path:
            return httpx.Response(200, json={"Id": IMAGE, "Config": {}})
        if request.url.path.endswith("/containers/json"):
            return httpx.Response(200, json=[])
        if request.method == "POST":
            return httpx.Response(201, json={"Id": "synthetic"})
        if request.method == "GET":
            return httpx.Response(200, json={"State": state})
        return httpx.Response(204)

    monkeypatch.setattr(executor.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handle))
    attach = AsyncMock(return_value=(0, '{"empty_store":true}', ""))
    monkeypatch.setattr(broker, "_attach", attach)
    return dict(image=IMAGE, socket="/trusted/docker.sock", docker="/trusted/docker"), requests, attach, state


@pytest.fixture
def credentials():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return dict(
        account_id="1234567_SB1",
        role_id="1234",
        certificate_id="synthetic-cert",
        private_key=key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
    )


@pytest.mark.asyncio
async def test_offline_fixed_operation_and_namespace(runtime, monkeypatch):
    args, requests, attach, _ = runtime
    monkeypatch.setenv("ENCRYPTION_KEY", "SYNTHETIC-WORKER-SECRET")
    await executor.verify_empty_store(**args)
    spec = json.loads(next(r.content for r in requests if r.method == "POST"))
    host = spec["HostConfig"]
    assert spec["User"] == "65532:65532" and spec["Cmd"] == []
    assert spec["Entrypoint"][:3] == ["/usr/local/bin/python", "-I", "-c"]
    assert spec["NetworkDisabled"] and host["NetworkMode"] == "none"
    assert host["CapDrop"] == ["ALL"] and "CapAdd" not in host
    assert host["Mounts"] == [] and host["ReadonlyRootfs"]
    assert set(host["Tmpfs"]) == {"/tmp"} and host["LogConfig"]["Type"] == "none"
    assert "SYNTHETIC-WORKER-SECRET" not in json.dumps(spec)
    assert json.loads(attach.call_args.args[1]) == {"operation": "verify_empty_store"}
    assert spec["Labels"][broker.LEASE_LABEL + ".purpose"] == "credentials"
    assert requests[-1].method == "DELETE" and requests[-1].url.params["v"] == "1"


@pytest.mark.asyncio
async def test_authentication_only_returns_exact_identity(runtime, credentials):
    args, requests, attach, _ = runtime
    expected = executor._identity(**credentials)
    attach.return_value = (0, json.dumps(asdict(expected)), "")
    assert await executor.authenticate(**credentials, **args) == expected
    spec = json.loads(next(r.content for r in requests if r.method == "POST"))
    assert not spec["NetworkDisabled"] and spec["HostConfig"]["NetworkMode"] == "bridge"
    assert "PRIVATE KEY" not in json.dumps(spec) and "synthetic-cert" not in json.dumps(spec)
    assert "private_key" in json.loads(attach.call_args.args[1])
    assert requests[-1].method == "DELETE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "other-sb1"),
        ("role_id", "9999"),
        ("requested_certificate_id", "other"),
        ("local_public_key_sha256", "0" * 64),
        ("extra", "secret"),
    ],
)
async def test_mismatched_or_extra_receipt_never_verifies(runtime, credentials, field, value):
    args, _, attach, _ = runtime
    receipt = asdict(executor._identity(**credentials))
    receipt[field] = value
    attach.return_value = (0, json.dumps(receipt), "")
    with pytest.raises(executor.CredentialExecutorError, match="identity"):
        await executor.authenticate(**credentials, **args)


@pytest.mark.asyncio
@pytest.mark.parametrize("image", ["", "latest", "registry/image:tag"])
async def test_mutable_image_never_contacts_daemon(runtime, image):
    args, requests, attach, _ = runtime
    args["image"] = image
    with pytest.raises(executor.CredentialExecutorError, match="immutable"):
        await executor.verify_empty_store(**args)
    assert not requests and attach.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "1234567"),
        ("role_id", "Administrator"),
        ("certificate_id", "../private"),
        ("private_key", b"not-a-key"),
    ],
)
async def test_invalid_identity_before_daemon(runtime, credentials, field, value):
    args, requests, _, _ = runtime
    credentials[field] = value
    with pytest.raises(executor.CredentialExecutorError):
        await executor.authenticate(**credentials, **args)
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt", ["not-json", "[]", '{"empty_store":1}', '{"empty_store":false}', '{"empty_store":true,"secret":"bad"}']
)
async def test_invalid_receipt_cannot_pass(runtime, receipt):
    args, requests, attach, _ = runtime
    attach.return_value = (0, receipt, "")
    with pytest.raises(executor.CredentialExecutorError):
        await executor.verify_empty_store(**args)
    assert requests[-1].method == "DELETE"


@pytest.mark.asyncio
async def test_timeout_preserves_category_and_removes(runtime):
    args, requests, attach, state = runtime
    state["ExitCode"] = 124
    with pytest.raises(TimeoutError):
        await executor.verify_empty_store(**args)
    assert requests[-1].method == "DELETE"
    state["ExitCode"] = 0
    attach.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await executor.verify_empty_store(**args)
    assert requests[-1].method == "DELETE"


@pytest.mark.asyncio
async def test_cancel_during_attach_cleans_namespace(runtime):
    args, requests, attach, _ = runtime
    started = asyncio.Event()

    async def wait(*_):
        started.set()
        await asyncio.Event().wait()

    attach.side_effect = wait
    task = asyncio.create_task(executor.verify_empty_store(**args))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert requests[-1].method == "DELETE"


@pytest.mark.asyncio
async def test_ambiguous_create_never_retries_or_attaches(runtime, monkeypatch):
    args, requests, attach, _ = runtime
    original = executor.httpx.AsyncHTTPTransport().handler

    def handle(request):
        if request.method == "POST":
            requests.append(request)
            raise httpx.ReadTimeout("private daemon message", request=request)
        return original(request)

    monkeypatch.setattr(executor.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handle))
    with pytest.raises(executor.CredentialExecutorError, match="reconcile") as caught:
        await executor.verify_empty_store(**args)
    assert "private daemon" not in str(caught.value)
    assert sum(r.method == "POST" for r in requests) == 1 and requests[-1].method == "DELETE"
    attach.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["preflight", "cleanup"])
async def test_failed_preflight_or_cleanup_cannot_pass(runtime, monkeypatch, phase):
    args, requests, attach, _ = runtime
    original = executor.httpx.AsyncHTTPTransport().handler

    def handle(request):
        if (phase == "preflight" and request.url.path.endswith("/containers/json")) or (
            phase == "cleanup" and request.method == "DELETE"
        ):
            requests.append(request)
            return httpx.Response(500, text="private daemon detail")
        return original(request)

    monkeypatch.setattr(executor.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handle))
    with pytest.raises(executor.CredentialExecutorError) as caught:
        await executor.verify_empty_store(**args)
    assert "private daemon" not in str(caught.value)
    if phase == "preflight":
        assert not any(r.method == "POST" for r in requests) and attach.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stderr", "output", "oom", "exit"])
async def test_failure_envelopes_never_pass(runtime, failure):
    args, requests, attach, state = runtime
    if failure == "stderr":
        attach.return_value = (0, '{"empty_store":true}', "private sdk error")
    if failure == "output":
        attach.return_value = (0, "x" * (executor.MAX_RECEIPT + 1), "")
    if failure == "oom":
        state["OOMKilled"] = True
    if failure == "exit":
        state["ExitCode"] = 125
    with pytest.raises(executor.CredentialExecutorError):
        await executor.verify_empty_store(**args)
    assert requests[-1].method == "DELETE"
