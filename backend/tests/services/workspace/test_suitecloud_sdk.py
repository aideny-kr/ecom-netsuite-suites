"""Synthetic protocol tests; these never authorize an actual NetSuite account."""

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.workspace import suitecloud_sdk as sdk


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    jar = tmp_path / "sdk.jar"
    jar.write_bytes(b"synthetic pinned artifact")
    monkeypatch.setattr(sdk, "SDK_SHA256", hashlib.sha256(jar.read_bytes()).hexdigest())
    return {"sdk_jar": jar, "java": Path(sys.executable)}


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def response(account="1234567_SB1", role="1234", **extra):
    return json.dumps(
        {
            "status": "SUCCESS",
            "data": {"accountInfo": {"companyId": account, "roleId": role, "companyName": "private label", **extra}},
        }
    ).encode()


@pytest.mark.asyncio
async def test_authentication_uses_private_home_pinned_copy_and_filtered_identity(runtime, private_key, monkeypatch):
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "untrusted inherited options")
    monkeypatch.setenv("SUITECLOUD_CI_PASSKEY", "ambient credential")
    captured = {}

    async def invoke(cmd, *, cwd, env, timeout):
        captured.update(cmd=cmd, cwd=cwd, env=env.copy())
        key = Path(cmd[cmd.index("-privatekeypath") + 1])
        assert key.read_bytes() == private_key
        assert key.stat().st_mode & 0o777 == 0o600
        assert key.parent.stat().st_mode & 0o777 == 0o700
        assert "JAVA_TOOL_OPTIONS" not in env
        assert env["SUITECLOUD_CI_PASSKEY"] != "ambient credential"
        assert len(env["SUITECLOUD_CI_PASSKEY"]) == 64
        assert env["SUITECLOUD_CI"] == "1"
        assert f"-Duser.home={cwd}" in cmd
        assert f"-Djava.io.tmpdir={cwd}" in cmd
        assert "-XX:+DisableAttachMechanism" in cmd
        assert "-XX:-UsePerfData" in cmd
        assert cmd[cmd.index("-jar") + 1] != str(runtime["sdk_jar"])
        assert "authenticateci" in cmd and "-authid" in cmd
        return response(entityId="private entity", token="private token")

    monkeypatch.setattr(sdk, "_run_process", invoke)
    async with sdk.IsolatedSdkSession(**runtime) as session:
        root = session.root
        assert root.stat().st_mode & 0o777 == 0o700
        identity = await session.authenticate(
            account_id="1234567-sb1", role_id="1234", certificate_id="synthetic-cert", private_key=private_key
        )
        assert identity.account_id == "1234567-sb1"
        assert identity.role_id == "1234"
        assert len(identity.local_public_key_sha256) == 64
        assert identity.requested_certificate_id == "synthetic-cert"
        assert "private token" not in repr(identity) and "private label" not in repr(identity)
        assert "PRIVATE KEY" not in repr(session)
        assert not (root / "private-key.pem").exists()
    assert not root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        response(account="7654321_SB1"),
        response(role="3"),
        response(role=None),
        response(role="Administrator"),
        b'{"status":"ERROR","errorMessages":["private token"]}',
        b"not-json private token",
        b'{"status":"SUCCESS","data":[]}',
        b'{"status":"SUCCESS","data":{"accountInfo":[]}}',
        b'{"status":"SUCCESS","errorMessages":["private token"],"data":{}}',
    ],
)
async def test_unverified_receipts_fail_closed_and_cannot_retry(runtime, private_key, monkeypatch, output):
    process = AsyncMock(return_value=output)
    monkeypatch.setattr(sdk, "_run_process", process)
    async with sdk.IsolatedSdkSession(**runtime) as session:
        with pytest.raises(sdk.SdkAuthError) as caught:
            await session.authenticate(
                account_id="1234567_SB1", role_id="1234", certificate_id="synthetic-cert", private_key=private_key
            )
        assert "private token" not in str(caught.value)
        assert not (session.root / "private-key.pem").exists()
        with pytest.raises(sdk.SdkAuthError, match="already attempted"):
            await session.authenticate(
                account_id="1234567_SB1", role_id="1234", certificate_id="synthetic-cert", private_key=private_key
            )
        assert process.await_count == 1


@pytest.mark.asyncio
async def test_two_sessions_never_share_auth_alias_passkey_or_home(runtime, private_key, monkeypatch):
    monkeypatch.setattr(sdk, "_run_process", AsyncMock(return_value=response()))
    async with sdk.IsolatedSdkSession(**runtime) as first, sdk.IsolatedSdkSession(**runtime) as second:
        assert first.root != second.root
        assert first.auth_id != second.auth_id
        assert first._env["SUITECLOUD_CI_PASSKEY"] != second._env["SUITECLOUD_CI_PASSKEY"]
        for session in (first, second):
            await session.authenticate(
                account_id="1234567_SB1", role_id="1234", certificate_id="synthetic-cert", private_key=private_key
            )


@pytest.mark.asyncio
async def test_bad_artifact_fails_before_any_process(runtime, monkeypatch):
    runtime["sdk_jar"].write_bytes(b"changed")
    process = AsyncMock()
    monkeypatch.setattr(sdk, "_run_process", process)
    with pytest.raises(sdk.SdkAuthError, match="artifact"):
        async with sdk.IsolatedSdkSession(**runtime):
            pytest.fail("unverified artifact entered")
    process.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_must_be_open_and_cannot_be_reopened(runtime, private_key):
    session = sdk.IsolatedSdkSession(**runtime)
    with pytest.raises(sdk.SdkAuthError, match="not open"):
        await session.authenticate(
            account_id="1234567_SB1", role_id="1234", certificate_id="synthetic-cert", private_key=private_key
        )
    async with session:
        root = session.root
    assert session._env == {} and not root.exists()
    with pytest.raises(sdk.SdkAuthError, match="cannot be reused"):
        async with session:
            pytest.fail("closed session reused")


@pytest.mark.asyncio
async def test_empty_store_probe_rejects_unexpected_credentials(runtime, monkeypatch):
    monkeypatch.setattr(sdk, "_run_process", AsyncMock(return_value=b'{"status":"SUCCESS","data":{"old":"secret"}}'))
    async with sdk.IsolatedSdkSession(**runtime) as session:
        with pytest.raises(sdk.SdkAuthError, match="not empty"):
            await session.verify_empty_store()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account,role,key",
    [
        ("1234567", "1234", None),
        ("1234567_SB1", "Administrator", None),
        ("1234567_SB1", "1234", b"not a private key"),
    ],
)
async def test_invalid_input_never_starts_sdk(runtime, private_key, monkeypatch, account, role, key):
    process = AsyncMock()
    monkeypatch.setattr(sdk, "_run_process", process)
    async with sdk.IsolatedSdkSession(**runtime) as session:
        with pytest.raises(sdk.SdkAuthError):
            await session.authenticate(
                account_id=account, role_id=role, certificate_id="synthetic-cert", private_key=key or private_key
            )
    process.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_auth_timeout_is_not_retried_and_root_is_removed(runtime, private_key, monkeypatch):
    process = AsyncMock(side_effect=asyncio.TimeoutError)
    monkeypatch.setattr(sdk, "_run_process", process)
    with pytest.raises(sdk.SdkAuthError, match="timed out"):
        async with sdk.IsolatedSdkSession(**runtime) as session:
            root = session.root
            await session.authenticate(
                account_id="1234567_SB1", role_id="1234", certificate_id="synthetic-cert", private_key=private_key
            )
    assert not root.exists()
    process.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_output_is_bounded(tmp_path):
    with pytest.raises(sdk.SdkAuthError, match="output limit"):
        await sdk._run_process(
            [sys.executable, "-c", "import sys;sys.stdout.write('x'*1000000);sys.stdout.flush()"],
            cwd=tmp_path,
            env={},
            timeout=5,
        )


@pytest.mark.asyncio
async def test_process_error_never_exposes_raw_output(tmp_path):
    with pytest.raises(sdk.SdkAuthError) as caught:
        await sdk._run_process(
            [sys.executable, "-c", "import sys;sys.stderr.write('private token');sys.exit(1)"],
            cwd=tmp_path,
            env={},
            timeout=5,
        )
    assert "private token" not in str(caught.value)


@pytest.mark.asyncio
async def test_detached_descendant_cannot_keep_cleanup_waiting_for_pipes(tmp_path):
    code = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c',\"import time;time.sleep(3)\"],start_new_session=True);"
        "time.sleep(30)"
    )
    start = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await sdk._run_process([sys.executable, "-c", code], cwd=tmp_path, env={}, timeout=0.25)
    assert time.monotonic() - start < 1.5


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_or_cancellation_terminates_descendants(tmp_path, cancel):
    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c',\"import time;time.sleep(60)\"]);"
        f"open({str(pid_file)!r},'w').write(str(p.pid));time.sleep(60)"
    )
    task = asyncio.create_task(
        sdk._run_process(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env={},
            timeout=0.5 if not cancel else 5,
        )
    )
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else asyncio.TimeoutError):
        await task
    pid = int(pid_file.read_text())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        # Linux container PID 1 may reap an orphan later. A zombie has
        # terminated and cannot use credentials or continue target work.
        stat = Path(f"/proc/{pid}/stat")
        if sys.platform == "linux" and stat.exists():
            try:
                if stat.read_text().rsplit(") ", 1)[1].split()[0] == "Z":
                    break
            except FileNotFoundError:
                break
        await asyncio.sleep(0.01)
    else:
        pytest.fail(f"SDK descendant {pid} survived cleanup")


@pytest.fixture
def reviewed_inputs():
    from app.services.workspace.snapshot import snapshot_hash

    files = {"SuiteScripts/exact.js": b"// exact\r\n", "tests/private.js": b"local-only"}
    manifest = [
        {
            "path": path,
            "operation": "create" if path.startswith("SuiteScripts/") else "unchanged",
            "content_sha": hashlib.sha256(content).hexdigest(),
            "apply_order": index,
        }
        for index, (path, content) in enumerate(files.items())
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


@pytest.mark.asyncio
async def test_server_validation_binds_exact_generated_bytes_and_filters_sdk_output(
    runtime, private_key, monkeypatch, reviewed_inputs
):
    from app.services.workspace.snapshot import materialized_snapshot_hash

    seen = {}

    async def invoke(cmd, *, cwd, env, timeout):
        if "authenticateci" in cmd:
            return response()
        seen["project"] = Path(cmd[cmd.index("-project") + 1])
        assert cmd[cmd.index("-jar") + 2 :] == [
            "validate",
            "-authid",
            seen["auth_id"],
            "-project",
            str(seen["project"]),
            "-server",
            "-accountspecificvalues",
            "ERROR",
        ]
        root = seen["project"]
        assert cwd != root and cwd == seen["home"]
        assert (root / "FileCabinet/SuiteScripts/exact.js").read_bytes() == b"// exact\r\n"
        assert not (root / "tests").exists()
        assert not list(root.rglob("*.pem")) and not (root / "package.json").exists()
        seen["sha"] = materialized_snapshot_hash(str(root))
        assert env["HOME"] == str(cwd) and timeout == 90
        return b'{"status":"SUCCESS","resultMessage":"private token","data":{"private":"label"}}'

    monkeypatch.setattr(sdk, "_run_process", invoke)
    async with sdk.IsolatedSdkSession(**runtime) as session:
        seen.update(auth_id=session.auth_id, home=session.root)
        identity = await session.authenticate(
            account_id="1234567_SB1", role_id="1234", certificate_id="synthetic-cert", private_key=private_key
        )
        receipt = await session.validate_project(expected_identity=identity, **reviewed_inputs)
        assert receipt.identity == identity
        assert receipt.source_snapshot_sha == reviewed_inputs["expected_snapshot_sha"]
        assert receipt.source_manifest_sha == reviewed_inputs["expected_manifest_sha"]
        assert receipt.project_sha == seen["sha"]
        assert receipt.project_format == "suite-studio-files-v1"
        assert len(receipt.sdk_output_sha256) == 64
        assert "private token" not in repr(receipt) and "label" not in repr(receipt)
        assert not seen["project"].exists()
        with pytest.raises(sdk.SdkAuthError, match="already attempted"):
            await session.validate_project(expected_identity=identity, **reviewed_inputs)
    assert not seen["home"].exists()


@pytest.mark.asyncio
async def test_validation_requires_successful_session_authentication(runtime, monkeypatch, reviewed_inputs):
    process = AsyncMock()
    monkeypatch.setattr(sdk, "_run_process", process)
    async with sdk.IsolatedSdkSession(**runtime) as session:
        with pytest.raises(sdk.SdkAuthError, match="verified authentication"):
            await session.validate_project(
                expected_identity=sdk.VerifiedIdentity("1234567-sb1", "1234", "cert", "a" * 64), **reviewed_inputs
            )
    process.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["wrong_identity", "bad_hash", "test_path", "unknown_format", "json_error", "timeout", "tamper", "cancel"],
)
async def test_validation_failure_never_returns_receipt_or_replays(
    runtime, private_key, monkeypatch, reviewed_inputs, failure
):
    from dataclasses import replace

    calls = []

    async def invoke(cmd, *, cwd, env, timeout):
        calls.append(cmd)
        if "authenticateci" in cmd:
            return response()
        root = Path(cmd[cmd.index("-project") + 1])
        calls.append(root)
        if failure == "json_error":
            return b'{"status":"ERROR","errorMessages":["private token"]}'
        if failure == "timeout":
            raise TimeoutError()
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "tamper":
            (root / "FileCabinet/SuiteScripts/exact.js").write_bytes(b"changed")
        return b'{"status":"SUCCESS"}'

    monkeypatch.setattr(sdk, "_run_process", invoke)
    async with sdk.IsolatedSdkSession(**runtime) as session:
        identity = await session.authenticate(
            account_id="1234567_SB1", role_id="1234", certificate_id="synthetic-cert", private_key=private_key
        )
        if failure == "wrong_identity":
            identity = replace(identity, role_id="9")
        if failure == "bad_hash":
            reviewed_inputs["expected_snapshot_sha"] = "0" * 64
        if failure == "test_path":
            reviewed_inputs["files"]["SuiteScripts/exact.test.js"] = reviewed_inputs["files"].pop(
                "SuiteScripts/exact.js"
            )
        if failure == "unknown_format":
            reviewed_inputs["project_format"] = "unknown"
        error = asyncio.CancelledError if failure == "cancel" else sdk.SdkAuthError
        with pytest.raises(error) as caught:
            await session.validate_project(expected_identity=identity, **reviewed_inputs)
        assert "private token" not in str(caught.value)
        with pytest.raises(sdk.SdkAuthError, match="already attempted"):
            await session.validate_project(expected_identity=identity, **reviewed_inputs)
        assert sum(isinstance(c, list) and "validate" in c for c in calls) == (
            failure in {"json_error", "timeout", "tamper", "cancel"}
        )
        for c in calls:
            if isinstance(c, Path):
                assert not c.exists()


@pytest.mark.asyncio
async def test_concurrent_validation_has_one_process_and_no_receipt_after_cancel(
    runtime, private_key, monkeypatch, reviewed_inputs
):
    started, release = asyncio.Event(), asyncio.Event()
    roots = []

    async def invoke(cmd, **kwargs):
        if "authenticateci" in cmd:
            return response()
        roots.append(Path(cmd[cmd.index("-project") + 1]))
        started.set()
        await release.wait()
        return b'{"status":"SUCCESS"}'

    monkeypatch.setattr(sdk, "_run_process", invoke)
    async with sdk.IsolatedSdkSession(**runtime) as session:
        identity = await session.authenticate(
            account_id="1234567_SB1", role_id="1234", certificate_id="synthetic-cert", private_key=private_key
        )
        first = asyncio.create_task(session.validate_project(expected_identity=identity, **reviewed_inputs))
        await asyncio.wait_for(started.wait(), 3)
        with pytest.raises(sdk.SdkAuthError, match="already attempted"):
            await session.validate_project(expected_identity=identity, **reviewed_inputs)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert len(roots) == 1 and not roots[0].exists()


@pytest.mark.asyncio
async def test_cancellation_during_filesystem_allocation_settles_and_removes_root(
    runtime, private_key, monkeypatch, reviewed_inputs
):
    import threading
    from contextlib import contextmanager

    started, release = threading.Event(), threading.Event()
    original = sdk.materialize_project
    roots = []

    @contextmanager
    def delayed(project):
        with original(project) as root:
            roots.append(root)
            started.set()
            assert release.wait(3)
            yield root

    process = AsyncMock(return_value=response())
    monkeypatch.setattr(sdk, "materialize_project", delayed)
    monkeypatch.setattr(sdk, "_run_process", process)
    async with sdk.IsolatedSdkSession(**runtime) as session:
        identity = await session.authenticate(
            account_id="1234567_SB1", role_id="1234", certificate_id="synthetic-cert", private_key=private_key
        )
        work = asyncio.create_task(session.validate_project(expected_identity=identity, **reviewed_inputs))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            work.cancel()
            await asyncio.sleep(0)
            work.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await work
        finally:
            release.set()
        process.assert_awaited_once()
        assert len(roots) == 1 and not roots[0].exists()
