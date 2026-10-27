"""Opt-in actual Docker checks; all accounts/keys are synthetic or absent.

CREDENTIAL_EXECUTOR_TEST_IMAGE is the locally built documented image ID.
CREDENTIAL_EXECUTOR_FAKE_IMAGE replaces only Java with a synthetic test double.
No authentication test here calls a NetSuite account or provisions credentials.
"""

import asyncio
import json
import os
import subprocess
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.workspace import command_sandbox as broker
from app.services.workspace import credential_executor as executor

IMAGE = os.environ.get("CREDENTIAL_EXECUTOR_TEST_IMAGE", "")
FAKE_IMAGE = os.environ.get("CREDENTIAL_EXECUTOR_FAKE_IMAGE", "")
SOCKET = os.environ.get("CREDENTIAL_EXECUTOR_TEST_SOCKET", "")
DOCKER = os.environ.get("CREDENTIAL_EXECUTOR_TEST_DOCKER", "")
COMMAND_IMAGE = os.environ.get("CREDENTIAL_EXECUTOR_TEST_COMMAND_IMAGE", "")
_CLIENT_CONFIG = ""
pytestmark = pytest.mark.skipif(
    not all((IMAGE, FAKE_IMAGE, SOCKET, DOCKER, COMMAND_IMAGE)), reason="owned Docker fixture not supplied"
)


@pytest.fixture(autouse=True)
def controlled_client(tmp_path, monkeypatch):
    config = tmp_path / "empty-docker-config"
    config.mkdir()
    monkeypatch.setitem(globals(), "_CLIENT_CONFIG", str(config))


def docker_cmd(*parts):
    return [DOCKER, "--host", "unix://" + SOCKET, "--config", _CLIENT_CONFIG, *parts]


def args(image=IMAGE):
    return dict(image=image, socket=SOCKET, docker=DOCKER)


def credentials(certificate="synthetic-cert"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return dict(
        account_id="1234567_SB1",
        role_id="1234",
        certificate_id=certificate,
        private_key=key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
    )


def containers():
    return subprocess.check_output(
        docker_cmd("ps", "-aq", "--no-trunc", "--filter", "label=" + broker.LEASE_LABEL + ".purpose=credentials"),
        text=True,
    ).split()


@pytest.mark.asyncio
async def test_actual_pinned_sdk_offline_store():
    before = containers()
    await executor.verify_empty_store(**args())
    assert containers() == before


@pytest.mark.asyncio
async def test_synthetic_identity_through_real_namespace(monkeypatch):
    monkeypatch.setenv("SDK_CANARY", "SYNTHETIC-HOST-SECRET")
    expected = credentials()
    before = containers()
    receipt = await executor.authenticate(**expected, **args(FAKE_IMAGE))
    assert receipt == executor._identity(**expected)
    assert containers() == before


@pytest.mark.asyncio
async def test_cancel_removes_private_store_and_detached_children():
    before = containers()
    task = asyncio.create_task(executor.authenticate(**credentials("synthetic-hang"), **args(FAKE_IMAGE)))
    found = None
    try:
        async with asyncio.timeout(15):
            while found is None:
                for cid in set(containers()) - set(before):
                    result = subprocess.run(
                        docker_cmd("exec", cid, "/bin/sh", "-c", "test -f /tmp/sdk-ready"),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    if result.returncode == 0:
                        found = cid
                        break
                await asyncio.sleep(0.1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert found is not None and containers() == before
    assert (
        subprocess.run(docker_cmd("inspect", found), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        != 0
    )


@pytest.mark.asyncio
async def test_worker_sigkill_deadline_survives_and_expired_lease_reaps(tmp_path):
    # Shortened controlled timer tests crash recovery without waiting two
    # minutes. Production uses the same fixed alarm with a 120-second budget.
    before = containers()
    key = credentials("synthetic-hang")
    script = r"""
import asyncio,sys
from app.services.workspace import credential_executor as e
e._BOOTSTRAP=e._BOOTSTRAP.replace("signal.alarm(120)", "signal.alarm(4)")
asyncio.run(e.authenticate(account_id="1234567_SB1",role_id="1234",certificate_id="synthetic-hang",
 private_key=sys.stdin.buffer.read(),image=sys.argv[1],socket=sys.argv[2],docker=sys.argv[3]))
"""
    import sys

    worker = subprocess.Popen(
        [sys.executable, "-c", script, FAKE_IMAGE, SOCKET, DOCKER],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    worker.stdin.write(key["private_key"])
    worker.stdin.close()
    found = None
    try:
        async with asyncio.timeout(15):
            while found is None:
                new = set(containers()) - set(before)
                if new:
                    cid = next(iter(new))
                    ready = subprocess.run(
                        docker_cmd("exec", cid, "/bin/sh", "-c", "test -f /tmp/sdk-ready"),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    if ready.returncode == 0:
                        found = cid
                await asyncio.sleep(0.1)
        worker.kill()
        worker.wait(timeout=5)
        async with asyncio.timeout(10):
            while True:
                metadata = json.loads(subprocess.check_output(docker_cmd("inspect", found), text=True))[0]
                if not metadata["State"]["Running"]:
                    break
                await asyncio.sleep(0.1)
        assert metadata["State"]["ExitCode"] == 124
        assert int(subprocess.check_output(docker_cmd("inspect", found, "--format", "{{.State.Pid}}"), text=True)) == 0

        # Reuse existing fixed lease logic at a controlled future time. No
        # running unrelated/legacy resource is made eligible by this test.
        class OnlyThisContainer:
            async def get(self, path, **kwargs):
                import httpx

                if path == "/containers/json":
                    return httpx.Response(200, json=[{"Id": found}], request=httpx.Request("GET", "http://docker"))
                return await client.get(path, **kwargs)

            async def delete(self, path, **kwargs):
                return await client.delete(path, **kwargs)

        import httpx

        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=SOCKET), base_url="http://docker/v1.45", trust_env=False
        ) as client:
            result = await broker._reconcile_expired(OnlyThisContainer(), now=int(time.time()) + 300, limit=100)
        assert result["removed"] == 1 and containers() == before
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        for owned_id in set(containers()) - set(before):
            subprocess.run(docker_cmd("rm", "-f", "-v", owned_id), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.mark.asyncio
async def test_tenant_code_cannot_read_live_credential_namespace(tmp_path):
    """Same numeric uid in two containers cannot cross the namespace boundary."""
    before = containers()
    credential_task = asyncio.create_task(executor.authenticate(**credentials("synthetic-hang"), **args(FAKE_IMAGE)))
    found = None
    try:
        async with asyncio.timeout(15):
            while found is None:
                for cid in set(containers()) - set(before):
                    ready = subprocess.run(
                        docker_cmd("exec", cid, "/bin/sh", "-c", "test -f /tmp/sdk-ready"),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    if ready.returncode == 0:
                        found = cid
                await asyncio.sleep(0.1)
        private_path = subprocess.check_output(
            docker_cmd("exec", found, "/bin/sh", "-c", "find /tmp -name private-key.pem"), text=True
        ).strip()
        assert private_path.startswith("/tmp/suitecloud_sdk_")
        (tmp_path / "probe.js").write_text(
            """const fs=require('fs');
for(const p of PATHS){try{fs.readFileSync(p);throw Error('credential visible')}catch(e){
 if(e.code!=='ENOENT'&&e.code!=='EACCES')throw e;}}
if(process.getuid()!==65532)throw Error('uid');
console.log('live credential namespace inaccessible');""".replace(
                "PATHS",
                json.dumps([private_path, "/tmp/sdk-ready", "/var/run/docker.sock"]),
            )
        )
        code, out, err = await broker.run_isolated_command(
            ["node", "probe.js"], str(tmp_path), 5, image=COMMAND_IMAGE, socket=SOCKET, docker=DOCKER
        )
        assert code == 0 and "namespace inaccessible" in out, (out, err)
    finally:
        credential_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await credential_task
    assert containers() == before


def reviewed_project():
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


def receipt_binding():
    import uuid

    return {
        k: str(uuid.uuid4())
        for k in ("tenant_id", "workspace_id", "changeset_id", "run_id", "actor_id", "connection_id")
    }


@pytest.mark.asyncio
async def test_actual_namespace_validates_exact_bytes_in_authenticated_session():
    from app.services.workspace.sdk_validation_receipt import verify_validation_receipt

    before = containers()
    key, project, context = credentials(), reviewed_project(), receipt_binding()
    envelope = await executor.validate_project(
        **key, **project, context=context, attempt_nonce="f" * 64, receipt_key=b"k" * 32, **args(FAKE_IMAGE)
    )
    result = verify_validation_receipt(
        envelope=json.loads(json.dumps(envelope)),
        identity=executor._identity(**key),
        inputs=project,
        context=context,
        attempt_nonce="f" * 64,
        image=FAKE_IMAGE,
        key=b"k" * 32,
    )
    assert result["server_validation"] is True
    assert "raw sdk detail" not in json.dumps(envelope) and "PRIVATE KEY" not in json.dumps(envelope)
    assert containers() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["base64", "schema", "snapshot", "reordered_negative_control"])
async def test_actual_child_rejects_corrupt_transport_before_authentication(bad, monkeypatch):
    import base64

    before = containers()
    # Observe attempted authentication rather than inferring ordering from the
    # final error. The negative control removes the early projection check:
    # validation still rejects, but this sentinel now detects authentication.
    marker = "SYNTHETIC_AUTH_ATTEMPT"
    needle = "        result = await session.authenticate("
    assert executor._BOOTSTRAP.count(needle) == 1
    bootstrap = executor._BOOTSTRAP.replace(needle, '        print("' + marker + '", flush=True)\n' + needle)
    if bad == "reordered_negative_control":
        bootstrap = bootstrap.replace("        build_ready_reviewed_project(**project)\n", "")
    monkeypatch.setattr(executor, "_BOOTSTRAP", bootstrap)
    observed = []
    original_attach = broker._attach

    async def observe(*args):
        result = await original_attach(*args)
        observed.append(result[1])
        return result

    monkeypatch.setattr(broker, "_attach", observe)
    key, project = credentials(), reviewed_project()
    project["files"] = {p: base64.b64encode(b).decode() for p, b in project["files"].items()}
    request = dict(operation="validate_project", **key, nonce="f" * 64, project=project)
    request["private_key"] = key["private_key"].decode()
    if bad == "base64":
        project["files"]["SuiteScripts/reviewed.js"] = "???"
    elif bad == "schema":
        request["argv"] = ["untrusted-command"]
    else:
        project["expected_snapshot_sha"] = "0" * 64
    with pytest.raises(executor.CredentialExecutorError):
        await executor._execute(request, **args(FAKE_IMAGE))
    assert len(observed) == 1
    assert (marker in observed[0]) == (bad == "reordered_negative_control")
    assert containers() == before


@pytest.mark.asyncio
async def test_cancel_during_actual_validation_removes_namespace():
    before = containers()
    task = asyncio.create_task(
        executor.validate_project(
            **credentials("synthetic-validation-hang"),
            **reviewed_project(),
            context=receipt_binding(),
            attempt_nonce="f" * 64,
            receipt_key=b"k" * 32,
            **args(FAKE_IMAGE),
        )
    )
    found = None
    try:
        async with asyncio.timeout(20):
            while found is None:
                for cid in set(containers()) - set(before):
                    if (
                        subprocess.run(
                            docker_cmd("exec", cid, "/bin/sh", "-c", "test -f /tmp/sdk-validation-ready"),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        ).returncode
                        == 0
                    ):
                        found = cid
                        break
                await asyncio.sleep(0.1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert found is not None and containers() == before


@pytest.mark.asyncio
async def test_actual_validation_transports_maximum_source_bytes(monkeypatch):
    import hashlib

    from app.services.workspace.snapshot import snapshot_hash
    from app.services.workspace.trusted_sdf_project import MAX_BYTES

    project = reviewed_project()
    bulk = b"x" * (MAX_BYTES - sum(map(len, project["files"].values())))
    project["files"]["SuiteScripts/bulk.txt"] = bulk
    project["manifest"].append(
        dict(
            path="SuiteScripts/bulk.txt",
            operation="create",
            content_sha=hashlib.sha256(bulk).hexdigest(),
            apply_order=2,
        )
    )
    project["expected_snapshot_sha"] = snapshot_hash(project["files"])
    project["expected_manifest_sha"] = hashlib.sha256(
        json.dumps(project["manifest"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    observed = []
    original_attach = broker._attach

    async def observe(cmd, data, timeout):
        observed.append(len(data))
        return await original_attach(cmd, data, timeout)

    monkeypatch.setattr(broker, "_attach", observe)
    before = containers()
    envelope = await executor.validate_project(
        **credentials(),
        **project,
        context=receipt_binding(),
        attempt_nonce="f" * 64,
        receipt_key=b"k" * 32,
        **args(FAKE_IMAGE),
    )
    assert MAX_BYTES < observed[0] <= executor.MAX_VALIDATION_INPUT
    assert envelope["validation"]["source_snapshot_sha"] == project["expected_snapshot_sha"]
    assert containers() == before


@pytest.mark.asyncio
async def test_actual_validation_namespace_deadline_is_sanitized_and_uncertain(monkeypatch):
    assert executor._BOOTSTRAP.count("signal.alarm(120)") == 1
    monkeypatch.setattr(executor, "_BOOTSTRAP", executor._BOOTSTRAP.replace("signal.alarm(120)", "signal.alarm(2)"))
    before = containers()
    with pytest.raises(executor.CredentialExecutorError, match="outcome uncertain after timeout; reconcile"):
        await executor.validate_project(
            **credentials("synthetic-validation-hang"),
            **reviewed_project(),
            context=receipt_binding(),
            attempt_nonce="f" * 64,
            receipt_key=b"k" * 32,
            **args(FAKE_IMAGE),
        )
    assert containers() == before
