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
    assert "LD_PRELOAD=" in spec["Env"] and "LD_LIBRARY_PATH=" in spec["Env"]
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


@pytest.fixture
def validation_inputs():
    import hashlib

    from app.services.workspace.snapshot import snapshot_hash

    files = {"SuiteScripts/reviewed.js": b"// exact\r\n\x00", "tests/local.js": b"private test input"}
    manifest = [
        dict(
            path=p,
            operation="create" if p.startswith("SuiteScripts/") else "unchanged",
            content_sha=hashlib.sha256(b).hexdigest(),
            apply_order=i,
        )
        for i, (p, b) in enumerate(files.items())
    ]
    return dict(
        files=files,
        manifest=manifest,
        expected_snapshot_sha=snapshot_hash(files),
        expected_manifest_sha=hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        project_format="suite-studio-files-v1",
    )


@pytest.fixture
def receipt_context():
    import uuid

    return {
        k: str(uuid.uuid4())
        for k in ("tenant_id", "workspace_id", "changeset_id", "run_id", "actor_id", "connection_id")
    }


def validation_result(credentials, inputs):
    from app.services.workspace.suitecloud_sdk import SDK_SHA256, SDK_VERSION, SdkValidationReceipt
    from app.services.workspace.trusted_sdf_project import build_ready_reviewed_project

    project = build_ready_reviewed_project(**inputs)
    return asdict(
        SdkValidationReceipt(
            executor._identity(**credentials),
            inputs["expected_snapshot_sha"],
            inputs["expected_manifest_sha"],
            inputs["project_format"],
            project.snapshot_sha,
            project.manifest_sha,
            project.project_sha,
            "e" * 64,
            SDK_VERSION,
            SDK_SHA256,
            True,
            "ERROR",
        )
    )


@pytest.mark.asyncio
async def test_reviewed_validation_transport_and_signed_roundtrip(
    runtime, credentials, validation_inputs, receipt_context
):
    import base64

    from app.services.workspace.sdk_validation_receipt import verify_validation_receipt

    args, requests, attach, _ = runtime
    result = validation_result(credentials, validation_inputs)
    attach.return_value = (0, json.dumps(dict(nonce="f" * 64, validation=result)), "")
    envelope = await executor.validate_project(
        **credentials,
        **validation_inputs,
        context=receipt_context,
        attempt_nonce="f" * 64,
        receipt_key=b"k" * 32,
        **args,
    )
    payload = json.loads(attach.call_args.args[1])
    assert {p: base64.b64decode(b) for p, b in payload["project"]["files"].items()} == validation_inputs["files"]
    assert payload["nonce"] == "f" * 64
    assert "receipt_key" not in payload and "context" not in payload and "mac" not in payload
    spec = json.loads(next(r.content for r in requests if r.method == "POST"))
    assert not spec["NetworkDisabled"] and spec["HostConfig"]["Mounts"] == []
    assert "private test input" not in json.dumps(spec) and "PRIVATE KEY" not in json.dumps(spec)
    assert requests[-1].method == "DELETE"
    assert "PRIVATE KEY" not in json.dumps(envelope) and "private test input" not in json.dumps(envelope)
    assert (
        verify_validation_receipt(
            envelope=json.loads(json.dumps(envelope)),
            identity=executor._identity(**credentials),
            inputs=validation_inputs,
            context=receipt_context,
            attempt_nonce="f" * 64,
            image=args["image"],
            key=b"k" * 32,
        )
        == result
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "nonce",
        "source_snapshot_sha",
        "source_manifest_sha",
        "projected_snapshot_sha",
        "projected_manifest_sha",
        "project_sha",
        "sdk_version",
        "sdk_sha256",
        "server_validation",
        "account_specific_values",
        "identity",
        "sdk_output_sha256",
        "extra",
    ],
)
async def test_validation_cannot_certify_wrong_child_result(
    runtime, credentials, validation_inputs, receipt_context, field
):
    args, requests, attach, _ = runtime
    result = validation_result(credentials, validation_inputs)
    outer = dict(nonce="f" * 64, validation=result)
    if field == "nonce":
        outer[field] = "0" * 64
    elif field == "server_validation":
        result[field] = 1
    else:
        result[field] = "wrong"
    attach.return_value = (0, json.dumps(outer), "")
    with pytest.raises(executor.AuthSeederError):
        await executor.validate_project(
            **credentials,
            **validation_inputs,
            context=receipt_context,
            attempt_nonce="f" * 64,
            receipt_key=b"k" * 32,
            **args,
        )
    assert requests[-1].method == "DELETE" and attach.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["snapshot", "manifest", "format", "files", "entry", "context", "nonce", "key"])
async def test_invalid_validation_inputs_never_contact_daemon(
    runtime, credentials, validation_inputs, receipt_context, bad
):
    args, requests, attach, _ = runtime
    params = dict(**validation_inputs, context=receipt_context, attempt_nonce="f" * 64, receipt_key=b"k" * 32)
    if bad == "snapshot":
        params["expected_snapshot_sha"] = "0" * 64
    elif bad == "manifest":
        params["manifest"][0]["content_sha"] = "0" * 64
    elif bad == "format":
        params["project_format"] = "unknown"
    elif bad == "files":
        params["files"] = []
    elif bad == "entry":
        params["manifest"] = [None]
    elif bad == "context":
        params["context"]["tenant_id"] = "not-uuid"
    elif bad == "nonce":
        params["attempt_nonce"] = "unsafe"
    elif bad == "key":
        params["receipt_key"] = b"weak"
    with pytest.raises(executor.AuthSeederError):
        await executor.validate_project(**credentials, **params, **args)
    assert requests == [] and attach.await_count == 0


@pytest.mark.parametrize(
    "changed",
    [
        "tenant_id",
        "workspace_id",
        "changeset_id",
        "run_id",
        "actor_id",
        "connection_id",
        "nonce",
        "image",
        "key",
        "tamper",
        "schema",
        "extra",
    ],
)
def test_signed_receipt_rejects_cross_context_replay(credentials, validation_inputs, receipt_context, changed):
    import uuid

    from app.services.workspace.sdk_validation_receipt import seal_validation_receipt, verify_validation_receipt

    params = dict(
        identity=executor._identity(**credentials),
        inputs=validation_inputs,
        context=receipt_context,
        attempt_nonce="f" * 64,
        image=IMAGE,
        key=b"k" * 32,
    )
    envelope = seal_validation_receipt(result=validation_result(credentials, validation_inputs), **params)
    if changed in receipt_context:
        params["context"] = {**receipt_context, changed: str(uuid.uuid4())}
    elif changed == "nonce":
        params["attempt_nonce"] = "a" * 64
    elif changed == "image":
        params["image"] = "sha256:" + "b" * 64
    elif changed == "key":
        params["key"] = b"z" * 32
    elif changed == "tamper":
        envelope["validation"]["sdk_output_sha256"] = "b" * 64
    elif changed == "schema":
        envelope["schema"] = "other-v1"
    else:
        envelope["extra"] = "untrusted"
    with pytest.raises(executor.AuthSeederError):
        verify_validation_receipt(envelope=envelope, **params)


@pytest.mark.asyncio
async def test_caller_edits_during_validation_do_not_change_signed_source(
    runtime, credentials, validation_inputs, receipt_context
):
    args, _, attach, _ = runtime
    original = validation_result(credentials, validation_inputs)

    async def invoke(*_):
        validation_inputs["files"].clear()
        validation_inputs["manifest"][0]["path"] = "evil.js"
        receipt_context["tenant_id"] = "mutated"
        return 0, json.dumps(dict(nonce="f" * 64, validation=original)), ""

    attach.side_effect = invoke
    envelope = await executor.validate_project(
        **credentials,
        **validation_inputs,
        context=receipt_context,
        attempt_nonce="f" * 64,
        receipt_key=b"k" * 32,
        **args,
    )
    assert envelope["validation"] == original and envelope["context"]["tenant_id"] != "mutated"


@pytest.mark.asyncio
async def test_validation_timeout_cannot_sign_or_replay(runtime, credentials, validation_inputs, receipt_context):
    args, requests, attach, _ = runtime
    attach.side_effect = TimeoutError("private daemon detail")
    with pytest.raises(executor.CredentialExecutorError, match="outcome uncertain after timeout") as caught:
        await executor.validate_project(
            **credentials,
            **validation_inputs,
            context=receipt_context,
            attempt_nonce="f" * 64,
            receipt_key=b"k" * 32,
            **args,
        )
    assert "private daemon detail" not in str(caught.value)
    assert attach.await_count == 1 and requests[-1].method == "DELETE"


@pytest.mark.asyncio
async def test_oversized_validation_transport_rejected_before_daemon(runtime):
    args, requests, attach, _ = runtime
    with pytest.raises(executor.CredentialExecutorError, match="input exceeds limit"):
        await executor._execute(dict(operation="validate_project", padding="x" * executor.MAX_VALIDATION_INPUT), **args)
    assert not requests and attach.await_count == 0
