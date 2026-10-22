"""Worker re-verification for deploy_sandbox runs.

Tests 19-20 in spec:
  19 — deploy_sandbox with matching expected_snapshot_sha proceeds to subprocess
  20 — deploy_sandbox with drifted snapshot fails with status=error, no subprocess

This is the codex P1 #7 belt-and-suspenders layer. The preview-confirm gate
already catches drift at HTTP time; this guards against the queue-backlog
window between confirm and worker pickup.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent
from app.models.workspace import (
    Workspace,
    WorkspaceChangeSet,
    WorkspaceFile,
    WorkspaceRun,
)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@pytest_asyncio.fixture
async def deploy_run_ready(db: AsyncSession, tenant_a, admin_user):
    """Create a queued deploy_sandbox run with a known snapshot."""
    user, _ = admin_user
    ws = Workspace(
        tenant_id=tenant_a.id,
        name="Worker re-verify WS",
        created_by=user.id,
        status="active",
    )
    db.add(ws)
    await db.flush()

    db.add(
        WorkspaceFile(
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
            path="SuiteScripts/deploy_me.js",
            file_name="deploy_me.js",
            content="console.log('to deploy');",
            sha256_hash=_sha256("console.log('to deploy');"),
            size_bytes=25,
            is_directory=False,
        )
    )
    cs = WorkspaceChangeSet(
        tenant_id=tenant_a.id,
        workspace_id=ws.id,
        title="Worker tests cs",
        status="approved",
        proposed_by=user.id,
    )
    db.add(cs)
    await db.flush()

    run = WorkspaceRun(
        tenant_id=tenant_a.id,
        workspace_id=ws.id,
        changeset_id=cs.id,
        run_type="deploy_sandbox",
        status="queued",
        triggered_by=user.id,
        correlation_id="test-corr-id",
    )
    db.add(run)
    await db.flush()
    from app.services.deploy_preview_service import compute_deploy_manifest

    snapshot = (await compute_deploy_manifest(db=db, changeset_id=cs.id, tenant_id=tenant_a.id, workspace_id=ws.id))[
        "snapshot_sha"
    ]
    for kind in ("suitecloud_validate", "jest_unit_test"):
        db.add(
            WorkspaceRun(
                tenant_id=tenant_a.id,
                workspace_id=ws.id,
                changeset_id=cs.id,
                run_type=kind,
                status="passed",
                gate_status="pass",
                snapshot_hash=snapshot,
                triggered_by=user.id,
            )
        )
    await db.flush()
    from app.models.workspace import WorkspaceArtifact

    validation = (
        await db.execute(
            select(WorkspaceRun).where(
                WorkspaceRun.changeset_id == cs.id, WorkspaceRun.run_type == "suitecloud_validate"
            )
        )
    ).scalar_one()
    from tests.workspace_identity import make_target

    _, _, identity = await make_target(db, tenant_a.id, "6738075-sb1")
    import json

    db.add(
        WorkspaceArtifact(
            tenant_id=tenant_a.id,
            run_id=validation.id,
            artifact_type="result_json",
            content=json.dumps(identity),
        )
    )
    await db.flush()
    return ws, cs, user, run


class TestWorkerSnapshotReverify:
    """Tests 19-20: deploy_sandbox worker re-verifies snapshot_sha."""

    @pytest.mark.asyncio
    async def test_19_matching_snapshot_proceeds_to_subprocess(self, db: AsyncSession, deploy_run_ready, tenant_a):
        """When expected_snapshot_sha matches what the worker computes,
        the worker proceeds to invoke suitecloud project:deploy (here
        mocked). Run reaches "passed" with exit_code 0."""
        from pathlib import Path

        from app.services import runner_service
        from app.services.deploy_preview_service import compute_deploy_manifest
        from app.services.workspace.suitecloud_auth_seeder import SeededCredentials

        ws, cs, user, run = deploy_run_ready

        # Compute the snapshot_sha the worker will produce so we know
        # what to pass as expected.
        manifest = await compute_deploy_manifest(
            db=db,
            changeset_id=cs.id,
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
        )
        expected_sha = manifest["snapshot_sha"]

        import json

        from app.models.workspace import WorkspaceArtifact

        artifact = await db.scalar(
            select(WorkspaceArtifact).where(
                WorkspaceArtifact.artifact_type == "result_json",
                WorkspaceArtifact.run_id.in_(
                    select(WorkspaceRun.id).where(
                        WorkspaceRun.changeset_id == cs.id, WorkspaceRun.run_type == "suitecloud_validate"
                    )
                ),
            )
        )
        identity = json.loads(artifact.content)

        # Mock the subprocess so the test doesn't actually call out.
        async def fake_subprocess(cmd, cwd, timeout):
            return 0, "deploy succeeded", ""

        with (
            patch("app.services.runner_service._run_subprocess", new=AsyncMock(side_effect=fake_subprocess)),
            patch(
                "app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run",
                new=AsyncMock(return_value=SeededCredentials(path=Path("/tmp/unused"), **identity)),
            ),
        ):
            result = await runner_service.execute_run(
                db,
                run.id,
                tenant_a.id,
                extra_params={
                    "sandbox_id": "6738075-sb1",
                    "expected_snapshot_sha": expected_sha,
                    "expected_manifest_sha": manifest["manifest_sha"],
                    "expected_target_identity": identity,
                },
            )

        assert result.status == "passed"
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_20_drifted_snapshot_fails_without_subprocess(self, db: AsyncSession, deploy_run_ready, tenant_a):
        """When expected_snapshot_sha disagrees with the worker's
        computation, the worker marks the run "error" and never invokes
        the subprocess. Audit event deploy.worker_snapshot_drift fires."""
        from app.services import runner_service

        ws, cs, user, run = deploy_run_ready

        wrong_expected = "0" * 64  # any 64-hex string that doesn't match

        # Track whether the subprocess was called — we want to assert NO.
        subprocess_called = False

        async def fake_subprocess(cmd, cwd, timeout):
            nonlocal subprocess_called
            subprocess_called = True
            return 0, "should never run", ""

        with patch(
            "app.services.runner_service._run_subprocess",
            new=AsyncMock(side_effect=fake_subprocess),
        ):
            result = await runner_service.execute_run(
                db,
                run.id,
                tenant_a.id,
                extra_params={
                    "sandbox_id": "6738075-sb1",
                    "expected_snapshot_sha": wrong_expected,
                    "expected_manifest_sha": "f" * 64,
                },
            )

        assert result.status == "error"
        assert subprocess_called is False, "Drift should abort before suitecloud project:deploy runs"

        # Audit event captures the drift reason.
        events = await db.execute(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant_a.id,
                AuditEvent.action == "deploy.worker_gate_rejected",
            )
        )
        event = events.scalar_one_or_none()
        assert event is not None
        assert event.payload["error_category"] == "DEPLOY_GATE"
