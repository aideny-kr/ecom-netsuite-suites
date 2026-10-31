"""Durable broker integration; synthetic SDK results never establish target acceptance."""

import asyncio
import json
import os
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.models.workspace import WorkspaceArtifact
from app.services import runner_service
from tests.services.workspace.test_content_bound_deploy import checked_project  # noqa: F401
from tests.workspace_identity import make_target


def synthetic_receipt(kwargs):
    from app.services.workspace import credential_executor
    from app.services.workspace.sdk_validation_receipt import seal_validation_receipt
    from app.services.workspace.suitecloud_sdk import SDK_SHA256, SDK_VERSION
    from app.services.workspace.trusted_sdf_project import build_ready_reviewed_project

    inputs = {
        k: kwargs[k] for k in ("files", "manifest", "expected_snapshot_sha", "expected_manifest_sha", "project_format")
    }
    project = build_ready_reviewed_project(**inputs)
    identity = credential_executor._identity(
        kwargs["account_id"], kwargs["role_id"], kwargs["certificate_id"], kwargs["private_key"]
    )
    return seal_validation_receipt(
        result=dict(
            identity=asdict(identity),
            source_snapshot_sha=inputs["expected_snapshot_sha"],
            source_manifest_sha=inputs["expected_manifest_sha"],
            project_format=inputs["project_format"],
            projected_snapshot_sha=project.snapshot_sha,
            projected_manifest_sha=project.manifest_sha,
            project_sha=project.project_sha,
            sdk_version=SDK_VERSION,
            sdk_sha256=SDK_SHA256,
            server_validation=True,
            account_specific_values="ERROR",
            sdk_output_sha256="f" * 64,
        ),
        identity=identity,
        inputs=inputs,
        context=kwargs["context"],
        attempt_nonce=kwargs["attempt_nonce"],
        image=kwargs["image"],
        key=kwargs["receipt_key"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("retained", [True, False])
async def test_rotation_reads_pinned_historical_key_without_resigning_or_replay(db, journal, retained):
    attempts, args, run, *_ = journal
    old = args["runtime"]
    with patch.object(
        attempts.credential_executor,
        "validate_project",
        AsyncMock(side_effect=lambda **kwargs: synthetic_receipt(kwargs)),
    ) as broker:
        await attempts.execute_validation_attempt(db, **args)
        original = await attempts._read(db, run, attempts.RECEIPT)
        claim = await attempts._read(db, run, attempts.ATTEMPT)
        assert claim["receipt_key_id"] == old.receipt_key_id
        rotated = attempts.ValidationRuntime(
            old.image, old.socket, old.docker, b"new-key-" * 8, (old.receipt_key,) if retained else ()
        )
        if retained:
            result = await attempts.read_validation_receipt(db, tenant_id=run.tenant_id, run_id=run.id, runtime=rotated)
            assert result == original["validation"]
        else:
            with pytest.raises(attempts.ValidationAttemptError):
                await attempts.read_validation_receipt(db, tenant_id=run.tenant_id, run_id=run.id, runtime=rotated)
        assert await attempts._read(db, run, attempts.RECEIPT) == original
        with pytest.raises(attempts.ValidationAttemptError, match="claimed"):
            await attempts.execute_validation_attempt(db, **{**args, "runtime": rotated})
        assert broker.await_count == 1
        assert run.gate_status == "block"
        assert not await db.scalar(
            select(WorkspaceArtifact.id).where(
                WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == "result_json"
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("key_size", [32, 64, 65, 256, 4096])
async def test_public_key_identifier_cannot_forge_persisted_receipt(db, journal, key_size):
    import hashlib

    from app.services.workspace.sdk_validation_receipt import _mac

    attempts, args, run, *_ = journal
    old = args["runtime"]
    runtime = attempts.ValidationRuntime(old.image, old.socket, old.docker, b"s" * key_size)
    with patch.object(
        attempts.credential_executor, "validate_project", AsyncMock(side_effect=lambda **kw: synthetic_receipt(kw))
    ):
        await attempts.execute_validation_attempt(db, **{**args, "runtime": runtime})
    row = await db.scalar(
        select(WorkspaceArtifact).where(
            WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == attempts.RECEIPT
        )
    )
    envelope = json.loads(row.content)
    claim = await attempts._read(db, run, attempts.ATTEMPT)
    # The tenant can read the claim and MAC. Treat that public identifier as an
    # attacker-controlled signing key, then store a checksum-consistent forgery.
    public_id = claim["receipt_key_id"]
    envelope["mac"] = _mac({k: v for k, v in envelope.items() if k != "mac"}, bytes.fromhex(public_id))
    row.content = attempts._content(envelope)
    row.size_bytes = len(row.content)
    row.sha256_hash = hashlib.sha256(row.content.encode()).hexdigest()
    await db.flush()
    with pytest.raises(attempts.AuthSeederError, match="does not match the expected attempt"):
        await attempts.read_validation_receipt(db, tenant_id=run.tenant_id, run_id=run.id, runtime=runtime)


@pytest.mark.asyncio
async def test_legacy_receipt_requires_original_active_key_and_keeps_v1_schema(db, journal):
    import hashlib

    attempts, args, run, *_ = journal
    old = args["runtime"]
    with patch.object(
        attempts.credential_executor,
        "validate_project",
        AsyncMock(side_effect=lambda **kwargs: synthetic_receipt(kwargs)),
    ) as broker:
        await attempts.execute_validation_attempt(db, **args)
        row = await db.scalar(
            select(WorkspaceArtifact).where(
                WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == attempts.ATTEMPT
            )
        )
        legacy = json.loads(row.content)
        legacy.pop("receipt_key_id")
        row.content = attempts._content(legacy)
        row.size_bytes = len(row.content)
        row.sha256_hash = hashlib.sha256(row.content.encode()).hexdigest()
        await db.flush()
        assert (await attempts._read(db, run, attempts.RECEIPT))["schema"] == "suite-studio-sdk-validation-v1"
        await attempts.read_validation_receipt(db, tenant_id=run.tenant_id, run_id=run.id, runtime=old)
        rotated = attempts.ValidationRuntime(old.image, old.socket, old.docker, b"new-key-" * 8, (old.receipt_key,))
        with pytest.raises(attempts.AuthSeederError, match="does not match the expected attempt"):
            await attempts.read_validation_receipt(db, tenant_id=run.tenant_id, run_id=run.id, runtime=rotated)
        assert broker.await_count == 1


@pytest_asyncio.fixture
async def journal(db, checked_project):  # noqa: F811 -- imported pytest fixture
    from app.services.workspace import sdk_validation_attempt as attempts

    ws, cs, user, *_ = checked_project
    connection, credentials, _ = await make_target(db, ws.tenant_id)
    run = await runner_service.create_run(db, ws.tenant_id, ws.id, "suitecloud_validate", user.id, changeset_id=cs.id)
    runtime = attempts.ValidationRuntime("sha256:" + "a" * 64, "/tmp/test.sock", "/usr/bin/docker", b"r" * 32)
    args = dict(tenant_id=ws.tenant_id, run_id=run.id, connection_id=connection.id, runtime=runtime)
    return attempts, args, run, connection, credentials, checked_project


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["actor", "company", "workspace", "changeset", "membership", "connection", "source"])
async def test_current_authority_and_source_checked_after_broker(db, journal, drift, tenant_b):
    from app.models.connection import Connection
    from app.models.tenant import Tenant
    from app.models.user import User, UserRole
    from app.models.workspace import Workspace, WorkspaceChangeSet, WorkspaceFile

    attempts, args, run, connection, _, project = journal
    ws, cs, user, *_ = project
    observed = []

    async def execute(**kwargs):
        result = synthetic_receipt(kwargs)
        model, identifier, changes = {
            "actor": (User, user.id, dict(is_active=False)),
            "company": (Tenant, ws.tenant_id, dict(is_active=False)),
            "workspace": (Workspace, ws.id, dict(status="archived")),
            "changeset": (WorkspaceChangeSet, cs.id, dict(status="rejected")),
            "connection": (Connection, connection.id, dict(status="revoked")),
            "source": (WorkspaceFile, project[3].id, dict(content="// concurrently changed\n")),
            "membership": (UserRole, None, dict(tenant_id=tenant_b.id)),
        }[drift]
        where = model.user_id == user.id if drift == "membership" else model.id == identifier
        if drift == "connection":
            connection.status = "revoked"
        else:
            await db.execute(update(model).where(where).values(**changes).execution_options(synchronize_session=False))
        await db.flush()
        observed.append(drift)
        return result

    with patch.object(attempts.credential_executor, "validate_project", AsyncMock(side_effect=execute)) as call:
        await attempts.execute_validation_attempt(db, **args)
        assert run.status == "failed" and run.gate_status == "block"
        with pytest.raises(attempts.AuthSeederError):
            await attempts.read_validation_receipt(
                db, tenant_id=args["tenant_id"], run_id=run.id, runtime=args["runtime"]
            )
        with pytest.raises(attempts.ValidationAttemptError, match="claimed"):
            await attempts.execute_validation_attempt(db, **args)
        assert call.await_count == 1
        assert observed == [drift]
    assert not (
        await db.scalars(
            select(WorkspaceArtifact).where(
                WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == attempts.RECEIPT
            )
        )
    ).all()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["mac", "nonce", "context", "image", "source"])
async def test_broker_tamper_is_durable_uncertain_and_never_replayed(db, journal, tamper):
    attempts, args, run, *_ = journal

    async def execute(**kwargs):
        envelope = synthetic_receipt(kwargs)
        if tamper == "mac":
            envelope["mac"] = "0" * 64
        elif tamper == "nonce":
            envelope["attempt_nonce"] = "0" * 64
        elif tamper == "context":
            envelope["context"]["actor_id"] = envelope["context"]["run_id"]
        elif tamper == "image":
            envelope["image"] = "sha256:" + "f" * 64
        else:
            envelope["validation"]["source_snapshot_sha"] = "f" * 64
        return envelope

    with patch.object(attempts.credential_executor, "validate_project", AsyncMock(side_effect=execute)) as call:
        await attempts.execute_validation_attempt(db, **args)
        assert run.status == "failed"
        outcome = await db.scalar(
            select(WorkspaceArtifact).where(
                WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == attempts.OUTCOME
            )
        )
        assert json.loads(outcome.content) == dict(state="uncertain", requires_reconciliation=True)
        with pytest.raises(attempts.ValidationAttemptError, match="claimed"):
            await attempts.execute_validation_attempt(db, **args)
        assert call.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("private-key=SYNTHETIC-DO-NOT-STORE"), TimeoutError(), asyncio.CancelledError()]
)
async def test_failure_or_cancel_consumes_claim_without_secret_diagnostics(db, journal, failure):
    attempts, args, run, *_ = journal
    with patch.object(attempts.credential_executor, "validate_project", AsyncMock(side_effect=failure)) as call:
        if isinstance(failure, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await attempts.execute_validation_attempt(db, **args)
            assert run.status == "running"
        else:
            await attempts.execute_validation_attempt(db, **args)
            assert run.status == "failed"
        with pytest.raises(attempts.ValidationAttemptError, match="claimed"):
            await attempts.execute_validation_attempt(db, **args)
        assert call.await_count == 1
    artifacts = (await db.scalars(select(WorkspaceArtifact).where(WorkspaceArtifact.run_id == run.id))).all()
    assert artifacts and "SYNTHETIC-DO-NOT-STORE" not in str([a.content for a in artifacts])
    assert not any(a.artifact_type in {"result_json", attempts.RECEIPT} for a in artifacts)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["image", "key", "nonce", "duplicate", "hash", "actor"])
async def test_persisted_receipt_reverified_against_current_independent_state(db, journal, drift):
    from app.models.user import User

    attempts, args, run, _, _, project = journal
    with patch.object(
        attempts.credential_executor, "validate_project", AsyncMock(side_effect=lambda **kw: synthetic_receipt(kw))
    ):
        await attempts.execute_validation_attempt(db, **args)
    runtime = args["runtime"]
    if drift in {"image", "key"}:
        runtime = attempts.ValidationRuntime(
            "sha256:" + ("b" if drift == "image" else "a") * 64,
            runtime.socket,
            runtime.docker,
            b"s" * 32 if drift == "key" else runtime.receipt_key,
        )
    elif drift == "actor":
        await db.execute(update(User).where(User.id == project[2].id).values(is_active=False))
    else:
        artifact = await db.scalar(
            select(WorkspaceArtifact).where(
                WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == attempts.ATTEMPT
            )
        )
        if drift == "hash":
            artifact.sha256_hash = "0" * 64
        elif drift == "duplicate":
            db.add(
                WorkspaceArtifact(
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    artifact_type=attempts.ATTEMPT,
                    content=artifact.content,
                    size_bytes=artifact.size_bytes,
                    sha256_hash=artifact.sha256_hash,
                )
            )
        else:
            import hashlib

            value = json.loads(artifact.content)
            value["attempt_nonce"] = "0" * 64
            artifact.content = attempts._content(value)
            artifact.size_bytes = len(artifact.content)
            artifact.sha256_hash = hashlib.sha256(artifact.content.encode()).hexdigest()
        await db.flush()
    with pytest.raises(attempts.AuthSeederError):
        await attempts.read_validation_receipt(db, tenant_id=args["tenant_id"], run_id=run.id, runtime=runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["actor", "company", "workspace", "changeset", "membership", "connection", "foreign"])
async def test_unauthorized_preflight_never_calls_broker_or_claims(db, journal, drift, tenant_b):
    from app.models.tenant import Tenant
    from app.models.user import UserRole

    attempts, args, run, connection, _, project = journal
    ws, cs, user, *_ = project
    if drift == "actor":
        user.is_active = False
    elif drift == "company":
        (await db.get(Tenant, ws.tenant_id)).is_active = False
    elif drift == "workspace":
        ws.status = "archived"
    elif drift == "changeset":
        cs.status = "rejected"
    elif drift == "connection":
        connection.status = "revoked"
    elif drift == "membership":
        await db.execute(update(UserRole).where(UserRole.user_id == user.id).values(tenant_id=tenant_b.id))
    else:
        connection.tenant_id = tenant_b.id
    await db.flush()
    with patch.object(attempts.credential_executor, "validate_project", AsyncMock()) as call:
        with pytest.raises(attempts.ValidationAttemptError):
            await attempts.execute_validation_attempt(db, **args)
        call.assert_not_awaited()
    assert run.status == "queued"
    assert not (await db.scalars(select(WorkspaceArtifact).where(WorkspaceArtifact.run_id == run.id))).all()


@pytest.mark.asyncio
async def test_malformed_persisted_claim_has_sanitized_error(db, journal):
    import hashlib

    attempts, args, run, *_ = journal
    with patch.object(
        attempts.credential_executor, "validate_project", AsyncMock(side_effect=lambda **kw: synthetic_receipt(kw))
    ):
        await attempts.execute_validation_attempt(db, **args)
    artifact = await db.scalar(
        select(WorkspaceArtifact).where(
            WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == attempts.ATTEMPT
        )
    )
    artifact.content = "{}"
    artifact.size_bytes = 2
    artifact.sha256_hash = hashlib.sha256(b"{}").hexdigest()
    await db.flush()
    with pytest.raises(attempts.ValidationAttemptError, match="journal"):
        await attempts.read_validation_receipt(db, tenant_id=args["tenant_id"], run_id=run.id, runtime=args["runtime"])


@pytest.mark.asyncio
async def test_attempt_commits_before_broker_and_verifies_persisted_receipt(db, journal):
    attempts, args, run, *_ = journal

    async def broker(**kwargs):
        assert (await db.get(type(run), run.id)).status == "running"
        record = await db.scalar(
            select(WorkspaceArtifact).where(
                WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == attempts.ATTEMPT
            )
        )
        assert json.loads(record.content)["attempt_nonce"] == kwargs["attempt_nonce"]
        return synthetic_receipt(kwargs)

    with patch.object(attempts.credential_executor, "validate_project", AsyncMock(side_effect=broker)) as call:
        result = await attempts.execute_validation_attempt(db, **args)
        assert result.status == "passed"
        assert result.gate_status == "block"  # Internal evidence cannot authorize deployment.
        evidence = await attempts.read_validation_receipt(
            db, tenant_id=args["tenant_id"], run_id=run.id, runtime=args["runtime"]
        )
        assert evidence["server_validation"] is True
        with pytest.raises(attempts.ValidationAttemptError, match="claimed"):
            await attempts.execute_validation_attempt(db, **args)
        assert call.await_count == 1


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("CREDENTIAL_EXECUTOR_FAKE_IMAGE"), reason="owned synthetic Docker image not supplied"
)
async def test_journal_through_actual_broker_and_disposable_namespace(db, journal):
    attempts, args, run, *_ = journal
    runtime = attempts.ValidationRuntime(
        os.environ["CREDENTIAL_EXECUTOR_FAKE_IMAGE"],
        os.environ["CREDENTIAL_EXECUTOR_TEST_SOCKET"],
        os.environ["CREDENTIAL_EXECUTOR_TEST_DOCKER"],
        b"r" * 32,
    )
    await attempts.execute_validation_attempt(db, **{**args, "runtime": runtime})
    assert run.status == "passed" and run.gate_status == "block"
    evidence = await attempts.read_validation_receipt(db, tenant_id=args["tenant_id"], run_id=run.id, runtime=runtime)
    assert evidence["server_validation"] is True
    # Java in this image is synthetic. No real account handshake is claimed.


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["source", "revocation"])
async def test_claim_commit_to_dispatch_recheck_prevents_credential_send(db, journal, drift, monkeypatch):
    from app.models.connection import Connection
    from app.models.workspace import WorkspaceFile

    attempts, args, run, connection, _, project = journal
    original_commit = attempts._commit
    injected = False

    async def commit_then_change(session, tenant_id):
        nonlocal injected
        await original_commit(session, tenant_id)
        if not injected:
            injected = True
            model = Connection if drift == "revocation" else WorkspaceFile
            identifier = connection.id if drift == "revocation" else project[3].id
            changes = dict(status="revoked") if drift == "revocation" else dict(content="// pre-dispatch edit\n")
            if drift == "revocation":
                row = await session.get(Connection, identifier)
                row.status = "revoked"
            else:
                await session.execute(update(model).where(model.id == identifier).values(**changes))
            await original_commit(session, tenant_id)

    monkeypatch.setattr(attempts, "_commit", commit_then_change)
    with patch.object(
        attempts.credential_executor, "validate_project", AsyncMock(side_effect=lambda **kw: synthetic_receipt(kw))
    ) as broker:
        await attempts.execute_validation_attempt(db, **args)
        broker.assert_not_awaited()
    assert injected and run.status == "failed" and run.gate_status == "block"
