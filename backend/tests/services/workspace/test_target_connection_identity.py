"""Current connection/key/certificate must still match the signed validation pin."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.encryption import encrypt_credentials
from app.models.workspace import WorkspaceArtifact
from app.services import runner_service
from app.services.deploy_preview_service import (
    DeployGateNotMetError,
    mint_deploy_token,
    verify_and_consume_deploy_token,
)
from tests.services.workspace.test_content_bound_deploy import checked_project, preview  # noqa: F401
from tests.workspace_identity import make_target


@pytest_asyncio.fixture
async def bound_project(db, checked_project):  # noqa: F811 -- imported pytest fixture
    ws = checked_project[0]
    connection, credentials, identity = await make_target(db, ws.tenant_id)
    artifact = await db.scalar(
        select(WorkspaceArtifact).where(
            WorkspaceArtifact.run_id == checked_project[-2].id, WorkspaceArtifact.artifact_type == "result_json"
        )
    )
    artifact.content = json.dumps({**json.loads(artifact.content), **identity})
    await db.flush()
    return checked_project, connection, credentials, identity, artifact


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["revoked", "superseded", "error", "provider", "tenant", "account", "role", "certificate", "key", "ciphertext"],
)
async def test_preview_rejects_current_connection_drift(db, bound_project, tenant_b, change):
    project, connection, credentials, identity, _ = bound_project
    if change in {"revoked", "superseded", "error"}:
        connection.status = change
    elif change == "provider":
        connection.provider = "stripe"
    elif change == "tenant":
        connection.tenant_id = tenant_b.id
    elif change == "ciphertext":
        connection.encrypted_credentials = "broken"
    else:
        if change == "account":
            credentials["account_id"] = "456-sb1"
        elif change == "role":
            credentials["suitecloud_ci"]["role_id"] = "5678"
        elif change == "certificate":
            credentials["suitecloud_ci"]["certificate_id"] = "replacement"
        elif change == "key":
            _, replacement, _ = await make_target(db, project[0].tenant_id)
            credentials["suitecloud_ci"]["private_key"] = replacement["suitecloud_ci"]["private_key"]
        connection.encrypted_credentials = encrypt_credentials(credentials)
    await db.flush()
    with pytest.raises(DeployGateNotMetError):
        await preview(db, project)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("connection_id", None),
        ("connection_id", "bad"),
        ("requested_certificate_id", None),
        ("requested_certificate_id", "\n"),
        ("local_public_key_sha256", None),
        ("local_public_key_sha256", "bad"),
    ],
)
async def test_legacy_or_malformed_identity_cannot_preview(db, bound_project, field, value):
    project, _, _, _, artifact = bound_project
    payload = json.loads(artifact.content)
    payload[field] = value
    artifact.content = json.dumps(payload)
    await db.flush()
    with pytest.raises(DeployGateNotMetError):
        await preview(db, project)


@pytest.mark.asyncio
async def test_confirmation_rechecks_current_certificate_without_consuming(db, bound_project):
    project, connection, credentials, identity, _ = bound_project
    ws, _, user, *_ = project
    p = await preview(db, project)
    minted = await mint_deploy_token(db=db, preview=p)
    credentials["suitecloud_ci"]["certificate_id"] = "replaced-after-preview"
    connection.encrypted_credentials = encrypt_credentials(credentials)
    await db.flush()
    with pytest.raises(DeployGateNotMetError):
        await verify_and_consume_deploy_token(
            db=db,
            jti=minted["jti"],
            confirmation_token=minted["confirmation_token"],
            actor_id=user.id,
            tenant_id=ws.tenant_id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["connection_id", "requested_certificate_id", "local_public_key_sha256"])
async def test_worker_rejects_adapter_identity_drift_before_command(db, bound_project, field):
    project, _, _, identity, _ = bound_project
    ws, cs, user, *_ = project
    p = await preview(db, project)
    run = await runner_service.create_run(db, ws.tenant_id, ws.id, "deploy_sandbox", user.id, changeset_id=cs.id)
    import uuid

    replacement = (
        str(uuid.uuid4())
        if field == "connection_id"
        else "0" * 64
        if field == "local_public_key_sha256"
        else "replacement"
    )
    changed = {**identity, field: replacement}
    seeded = SimpleNamespace(path=Path("/tmp/unused"), **changed)
    with (
        patch.object(runner_service, "_run_subprocess", AsyncMock(return_value=(0, "", ""))) as process,
        patch("app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run", AsyncMock(return_value=seeded)),
    ):
        await runner_service.execute_run(
            db,
            run.id,
            ws.tenant_id,
            extra_params={
                "sandbox_id": "123-sb1",
                "expected_snapshot_sha": p["snapshot_sha"],
                "expected_manifest_sha": p["manifest_sha"],
                "expected_target_identity": p["target_identity"],
            },
        )
    assert run.status == "error"
    process.assert_not_awaited()


@pytest.mark.asyncio
async def test_healthy_explicit_connection_is_pinned_without_secrets(db, bound_project):
    project, connection, _, identity, _ = bound_project
    connection.status = "healthy"
    await db.flush()
    p = await preview(db, project)
    assert p["target_identity"] == identity
    assert "private_key" not in json.dumps(p)


@pytest.mark.asyncio
async def test_account_role_only_preview_replaced_immediately(db, bound_project):
    import uuid

    from app.models.workspace import WorkspaceDeployToken

    project, _, _, identity, _ = bound_project
    old = await mint_deploy_token(db=db, preview=await preview(db, project))
    token = await db.get(WorkspaceDeployToken, uuid.UUID(old["jti"]))
    token.target_identity = {"account_id": identity["account_id"], "role_id": identity["role_id"]}
    await db.flush()
    replacement = await mint_deploy_token(db=db, preview=await preview(db, project))
    assert replacement["jti"] != old["jti"]
    assert token.consumed_reason == "superseded"


@pytest.mark.asyncio
async def test_revoked_connection_cannot_borrow_another_available_connection(db, bound_project):
    project, connection, credentials, identity, _ = bound_project
    await make_target(db, project[0].tenant_id)
    connection.status = "revoked"
    connection.metadata_json = identity
    await db.flush()
    with pytest.raises(DeployGateNotMetError):
        await preview(db, project)


@pytest.mark.asyncio
async def test_worker_rechecks_revocation_after_confirmation_before_seeding(db, bound_project):
    project, connection, _, _, _ = bound_project
    ws, cs, user, *_ = project
    p = await preview(db, project)
    run = await runner_service.create_run(db, ws.tenant_id, ws.id, "deploy_sandbox", user.id, changeset_id=cs.id)
    connection.status = "revoked"
    await db.flush()
    with (
        patch.object(runner_service, "_run_subprocess", AsyncMock()) as process,
        patch("app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run", AsyncMock()) as seeder,
    ):
        await runner_service.execute_run(
            db,
            run.id,
            ws.tenant_id,
            extra_params={
                "sandbox_id": "123-sb1",
                "expected_snapshot_sha": p["snapshot_sha"],
                "expected_manifest_sha": p["manifest_sha"],
                "expected_target_identity": p["target_identity"],
            },
        )
    assert run.status == "error"
    process.assert_not_awaited()
    seeder.assert_not_awaited()


@pytest.mark.asyncio
async def test_validation_does_not_launch_with_stale_adapter_certificate(db, bound_project):
    project, _, _, identity, _ = bound_project
    ws, cs, user, *_ = project
    run = await runner_service.create_run(db, ws.tenant_id, ws.id, "suitecloud_validate", user.id, changeset_id=cs.id)
    stale = SimpleNamespace(path=Path("/tmp/unused"), **{**identity, "requested_certificate_id": "obsolete"})
    with (
        patch.object(runner_service, "_run_subprocess", AsyncMock()) as process,
        patch("app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run", AsyncMock(return_value=stale)),
    ):
        await runner_service.execute_run(db, run.id, ws.tenant_id)
    assert run.status == "failed" and run.gate_status == "block"
    process.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_success_receipt_preserves_bound_identity(db, bound_project):
    from app.models.audit import AuditEvent

    project, _, _, identity, _ = bound_project
    ws, cs, user, *_ = project
    p = await preview(db, project)
    run = await runner_service.create_run(db, ws.tenant_id, ws.id, "deploy_sandbox", user.id, changeset_id=cs.id)
    with (
        patch.object(runner_service, "_run_subprocess", AsyncMock(return_value=(0, "", ""))),
        patch(
            "app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run",
            AsyncMock(return_value=SimpleNamespace(path=Path("/tmp/unused"), **identity)),
        ),
    ):
        await runner_service.execute_run(
            db,
            run.id,
            ws.tenant_id,
            extra_params={
                "sandbox_id": "123-sb1",
                "expected_snapshot_sha": p["snapshot_sha"],
                "expected_manifest_sha": p["manifest_sha"],
                "expected_target_identity": p["target_identity"],
            },
        )
    assert run.status == "passed"
    result = await db.scalar(
        select(WorkspaceArtifact).where(
            WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == "result_json"
        )
    )
    assert json.loads(result.content)["target_identity"] == identity
    audit = await db.scalar(
        select(AuditEvent).where(AuditEvent.resource_id == str(run.id), AuditEvent.action == "run_succeeded")
    )
    assert audit.payload["target_identity"] == identity


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError("synthetic transport failure")])
async def test_uncertain_deploy_retains_verified_identity(db, bound_project, failure):
    project, _, _, identity, _ = bound_project
    ws, cs, user, *_ = project
    p = await preview(db, project)
    run = await runner_service.create_run(db, ws.tenant_id, ws.id, "deploy_sandbox", user.id, changeset_id=cs.id)
    with (
        patch.object(runner_service, "_run_subprocess", AsyncMock(side_effect=failure)),
        patch(
            "app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run",
            AsyncMock(return_value=SimpleNamespace(path=Path("/tmp/unused"), **identity)),
        ),
    ):
        await runner_service.execute_run(
            db,
            run.id,
            ws.tenant_id,
            extra_params={
                "sandbox_id": "123-sb1",
                "expected_snapshot_sha": p["snapshot_sha"],
                "expected_manifest_sha": p["manifest_sha"],
                "expected_target_identity": p["target_identity"],
            },
        )
    artifact = await db.scalar(
        select(WorkspaceArtifact).where(
            WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == "result_json"
        )
    )
    result = json.loads(artifact.content)
    assert result["target_identity"] == identity
    assert result["target_identity_verification"] == "verified_before_command"
    assert result["external_outcome"] == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["suitecloud_validate", "deploy_sandbox"])
async def test_connection_rotation_during_command_cannot_certify_success(db, bound_project, kind):
    project, connection, credentials, identity, _ = bound_project
    ws, cs, user, *_ = project
    p = await preview(db, project)
    run = await runner_service.create_run(db, ws.tenant_id, ws.id, kind, user.id, changeset_id=cs.id)

    async def rotate(*_):
        credentials["suitecloud_ci"]["certificate_id"] = "rotated-during-command"
        connection.encrypted_credentials = encrypt_credentials(credentials)
        await db.flush()
        return 0, "", ""

    with (
        patch.object(runner_service, "_run_subprocess", AsyncMock(side_effect=rotate)),
        patch(
            "app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run",
            AsyncMock(return_value=SimpleNamespace(path=Path("/tmp/unused"), **identity)),
        ),
    ):
        await runner_service.execute_run(
            db,
            run.id,
            ws.tenant_id,
            extra_params={
                "sandbox_id": "123-sb1",
                "expected_snapshot_sha": p["snapshot_sha"],
                "expected_manifest_sha": p["manifest_sha"],
                "expected_target_identity": p["target_identity"],
            },
        )
    assert run.status in {"error", "failed"}
    artifact = await db.scalar(
        select(WorkspaceArtifact).where(
            WorkspaceArtifact.run_id == run.id, WorkspaceArtifact.artifact_type == "result_json"
        )
    )
    assert json.loads(artifact.content)["external_outcome"] == "uncertain"


@pytest.mark.asyncio
async def test_rotated_full_identity_preview_can_be_replaced(db, bound_project):
    import uuid

    from app.models.workspace import WorkspaceDeployToken

    project, connection, credentials, identity, artifact = bound_project
    old = await mint_deploy_token(db=db, preview=await preview(db, project))
    credentials["suitecloud_ci"]["certificate_id"] = "new-certificate"
    connection.encrypted_credentials = encrypt_credentials(credentials)
    receipt = json.loads(artifact.content)
    receipt["requested_certificate_id"] = "new-certificate"
    artifact.content = json.dumps(receipt)
    await db.flush()
    new = await mint_deploy_token(db=db, preview=await preview(db, project))
    token = await db.get(WorkspaceDeployToken, uuid.UUID(old["jti"]))
    assert token.consumed_reason == "superseded" and new["jti"] != old["jti"]


@pytest.mark.asyncio
async def test_unsupported_key_algorithm_is_a_safe_gate_failure(db, bound_project):
    from cryptography.exceptions import UnsupportedAlgorithm

    project = bound_project[0]
    with patch(
        "app.services.workspace.target_identity.serialization.load_pem_private_key",
        side_effect=UnsupportedAlgorithm("synthetic"),
    ):
        with pytest.raises(DeployGateNotMetError):
            await preview(db, project)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["permission", "approval", "content"])
async def test_worker_rechecks_authority_after_credential_preparation(db, bound_project, drift):
    from app.models.user import User

    project, _, _, identity, _ = bound_project
    ws, cs, user, file, *_ = project
    p = await preview(db, project)
    run = await runner_service.create_run(db, ws.tenant_id, ws.id, "deploy_sandbox", user.id, changeset_id=cs.id)

    async def seed(**_):
        if drift == "permission":
            actor = await db.get(User, user.id)
            actor.is_active = False
        elif drift == "approval":
            cs.status = "draft"
        else:
            file.content = "// changed during auth\n"
        await db.flush()
        return SimpleNamespace(path=Path("/tmp/unused"), **identity)

    with (
        patch.object(runner_service, "_run_subprocess", AsyncMock(return_value=(0, "", ""))) as command,
        patch("app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run", AsyncMock(side_effect=seed)),
    ):
        await runner_service.execute_run(
            db,
            run.id,
            ws.tenant_id,
            extra_params={
                "sandbox_id": "123-sb1",
                "expected_snapshot_sha": p["snapshot_sha"],
                "expected_manifest_sha": p["manifest_sha"],
                "expected_target_identity": p["target_identity"],
            },
        )
    assert run.status == "error"
    command.assert_not_awaited()
