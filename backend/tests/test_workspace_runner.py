"""Unit tests for workspace runner: command allowlist, lifecycle, artifacts, tenant isolation, audit, MCP."""

import hashlib
import io
import json
import uuid
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import runner_service
from app.services.runner_service import CommandNotAllowedError, validate_run_type
from tests.conftest import create_test_tenant, create_test_user

# --- Fixtures ---


@pytest_asyncio.fixture
async def tenant(db: AsyncSession):
    t = await create_test_tenant(db, name="Runner Test Corp", plan="pro")
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{t.id}'"))
    return t


@pytest_asyncio.fixture
async def user(db: AsyncSession, tenant):
    u, _ = await create_test_user(db, tenant, role_name="admin")
    return u


@pytest_asyncio.fixture
async def workspace(db: AsyncSession, tenant, user):
    from app.services import workspace_service as ws_svc

    ws = await ws_svc.create_workspace(db, tenant.id, "Runner Workspace", user.id, "Test workspace for runner")
    return ws


@pytest_asyncio.fixture
async def changeset(db: AsyncSession, workspace, tenant, user):
    from app.services import workspace_service as ws_svc

    cs = await ws_svc.create_changeset(db, workspace.id, tenant.id, "Test changeset", user.id)
    return cs


# --- TestCommandAllowlist ---


class TestCommandAllowlist:
    def test_valid_sdf_validate(self):
        config = validate_run_type("sdf_validate")
        assert config["cmd"] == ["sdf", "validate"]
        assert config["timeout"] == 60

    def test_valid_jest(self):
        config = validate_run_type("jest_unit_test")
        assert config["cmd"] == ["npx", "jest", "--json", "--coverage"]
        assert config["timeout"] == 120

    def test_invalid_type_rejected(self):
        with pytest.raises(CommandNotAllowedError, match="Invalid run_type"):
            validate_run_type("rm_rf")

    def test_shell_injection_rejected(self):
        with pytest.raises(CommandNotAllowedError):
            validate_run_type("sdf_validate; rm -rf /")


# --- TestRunLifecycle ---


class TestRunLifecycle:
    @pytest.mark.asyncio
    async def test_create_run_queued(self, db, tenant, user, workspace):
        run = await runner_service.create_run(db, tenant.id, workspace.id, "sdf_validate", user.id)
        assert run.status == "queued"
        assert run.run_type == "sdf_validate"
        assert run.workspace_id == workspace.id
        assert run.correlation_id is not None

    @pytest.mark.asyncio
    async def test_execute_run_passed(self, db, tenant, user, workspace):
        run = await runner_service.create_run(db, tenant.id, workspace.id, "sdf_validate", user.id)
        await db.flush()

        with patch.object(
            runner_service,
            "_run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, "Validation passed\n", ""),
        ):
            result = await runner_service.execute_run(db, run.id, tenant.id)

        assert result.status == "passed"
        assert result.exit_code == 0
        assert result.duration_ms is not None
        assert result.started_at is not None
        assert result.completed_at is not None

    @pytest.mark.asyncio
    async def test_execute_run_failed(self, db, tenant, user, workspace):
        run = await runner_service.create_run(db, tenant.id, workspace.id, "sdf_validate", user.id)
        await db.flush()

        with patch.object(
            runner_service,
            "_run_subprocess",
            new_callable=AsyncMock,
            return_value=(1, "", "Error: validation failed\n"),
        ):
            result = await runner_service.execute_run(db, run.id, tenant.id)

        assert result.status == "failed"
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_changeset_overlay_is_materialized(self, db, tenant, user, workspace):
        from app.services import workspace_service as ws_svc

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("src/app.ts", "export const value = 'old';\n")
        await ws_svc.import_workspace(db, workspace.id, tenant.id, buf.getvalue())

        propose_result = await ws_svc.propose_patch(
            db,
            workspace.id,
            tenant.id,
            "src/app.ts",
            (
                "--- a/src/app.ts\n"
                "+++ b/src/app.ts\n"
                "@@ -1 +1 @@\n"
                "-export const value = 'old';\n"
                "+export const value = 'new';\n"
            ),
            "Update value",
            user.id,
        )
        cs_id = uuid.UUID(propose_result["changeset_id"])
        await ws_svc.transition_changeset(db, cs_id, tenant.id, "submit", user.id)
        await ws_svc.transition_changeset(db, cs_id, tenant.id, "approve", user.id)

        run = await runner_service.create_run(
            db,
            tenant.id,
            workspace.id,
            "sdf_validate",
            user.id,
            changeset_id=cs_id,
        )
        await db.flush()

        async def _assert_overlay(cmd, cwd, timeout):  # noqa: ARG001
            content = Path(cwd, "src/app.ts").read_text(encoding="utf-8")
            return 0, content, ""

        with patch.object(runner_service, "_run_subprocess", new_callable=AsyncMock, side_effect=_assert_overlay):
            await runner_service.execute_run(db, run.id, tenant.id)

        artifacts = await runner_service.get_artifacts(db, run.id, tenant.id)
        stdout_artifact = next((a for a in artifacts if a.artifact_type == "stdout"), None)
        assert stdout_artifact is not None
        assert "'new'" in (stdout_artifact.content or "")


# --- TestArtifactImmutability ---


class TestArtifactImmutability:
    @pytest.mark.asyncio
    async def test_artifacts_have_correct_sha256(self, db, tenant, user, workspace):
        run = await runner_service.create_run(db, tenant.id, workspace.id, "sdf_validate", user.id)
        await db.flush()

        stdout_content = "Validation passed\n"
        with patch.object(
            runner_service,
            "_run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, stdout_content, ""),
        ):
            await runner_service.execute_run(db, run.id, tenant.id)

        artifacts = await runner_service.get_artifacts(db, run.id, tenant.id)
        assert len(artifacts) >= 1

        stdout_artifact = next((a for a in artifacts if a.artifact_type == "stdout"), None)
        assert stdout_artifact is not None
        expected_hash = hashlib.sha256(stdout_content.encode("utf-8")).hexdigest()
        assert stdout_artifact.sha256_hash == expected_hash
        assert stdout_artifact.size_bytes == len(stdout_content.encode("utf-8"))

    @pytest.mark.asyncio
    async def test_stdout_and_stderr_present(self, db, tenant, user, workspace):
        run = await runner_service.create_run(db, tenant.id, workspace.id, "jest_unit_test", user.id)
        await db.flush()

        with patch.object(
            runner_service,
            "_run_subprocess",
            new_callable=AsyncMock,
            return_value=(1, "test output", "test error"),
        ):
            await runner_service.execute_run(db, run.id, tenant.id)

        artifacts = await runner_service.get_artifacts(db, run.id, tenant.id)
        types = {a.artifact_type for a in artifacts}
        assert "stdout" in types
        assert "stderr" in types

    @pytest.mark.asyncio
    async def test_result_json_artifact_always_present(self, db, tenant, user, workspace):
        run = await runner_service.create_run(db, tenant.id, workspace.id, "sdf_validate", user.id)
        await db.flush()

        with patch.object(
            runner_service,
            "_run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, "", ""),
        ):
            await runner_service.execute_run(db, run.id, tenant.id)

        artifacts = await runner_service.get_artifacts(db, run.id, tenant.id)
        assert any(a.artifact_type == "result_json" for a in artifacts)

    @pytest.mark.asyncio
    async def test_redaction_and_log_capping(self, db, tenant, user, workspace):
        run = await runner_service.create_run(db, tenant.id, workspace.id, "sdf_validate", user.id)
        await db.flush()

        huge = "x" * (runner_service.MAX_ARTIFACT_BYTES + 500)
        stdout = f"token=abc123\nauthorization: bearer super-secret-value\n{huge}"
        with patch.object(
            runner_service,
            "_run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, stdout, ""),
        ):
            await runner_service.execute_run(db, run.id, tenant.id)

        artifacts = await runner_service.get_artifacts(db, run.id, tenant.id)
        stdout_artifact = next((a for a in artifacts if a.artifact_type == "stdout"), None)
        assert stdout_artifact is not None
        assert "abc123" not in (stdout_artifact.content or "")
        assert "super-secret-value" not in (stdout_artifact.content or "")
        assert stdout_artifact.size_bytes <= runner_service.MAX_ARTIFACT_BYTES + len(
            runner_service.TRUNCATED_SUFFIX.encode("utf-8")
        )
        assert "...[TRUNCATED]" in (stdout_artifact.content or "")

    @pytest.mark.asyncio
    async def test_unit_test_run_stores_report_and_coverage(self, db, tenant, user, workspace):
        run = await runner_service.create_run(db, tenant.id, workspace.id, "jest_unit_test", user.id)
        await db.flush()

        async def _jest_subprocess(cmd, cwd, timeout):  # noqa: ARG001
            coverage_dir = Path(cwd) / "coverage"
            coverage_dir.mkdir(parents=True, exist_ok=True)
            coverage_dir.joinpath("coverage-summary.json").write_text(
                json.dumps({"total": {"lines": {"pct": 90.5}}}),
                encoding="utf-8",
            )
            return 0, json.dumps({"numTotalTests": 3, "numPassedTests": 3}), ""

        with patch.object(runner_service, "_run_subprocess", new_callable=AsyncMock, side_effect=_jest_subprocess):
            await runner_service.execute_run(db, run.id, tenant.id)

        artifacts = await runner_service.get_artifacts(db, run.id, tenant.id)
        types = {a.artifact_type for a in artifacts}
        assert "report_json" in types
        assert "coverage_json" in types


# --- TestTenantIsolation ---


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_tenant_a_run_invisible_to_tenant_b(self, db, user, workspace, tenant):
        # Create run for tenant A
        run = await runner_service.create_run(db, tenant.id, workspace.id, "sdf_validate", user.id)
        await db.flush()

        # Create tenant B
        tenant_b = await create_test_tenant(db, name="Tenant B", plan="pro")

        # Switch RLS context to tenant B
        await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_b.id}'"))

        # tenant B should not see tenant A's run
        result = await runner_service.get_run(db, run.id, tenant_b.id)
        assert result is None


# --- TestAuditEvents ---


class TestAuditEvents:
    @pytest.mark.asyncio
    async def test_run_triggered_audit_via_api(self, db, tenant, user, workspace, changeset, client):
        from types import ModuleType
        from unittest.mock import MagicMock

        from app.core.security import create_access_token
        from app.services import workspace_service as ws_svc

        await ws_svc.transition_changeset(db, changeset.id, tenant.id, "submit", user.id)
        await ws_svc.transition_changeset(db, changeset.id, tenant.id, "approve", user.id)

        token = create_access_token({"sub": str(user.id), "tenant_id": str(tenant.id)})

        # Create a fake module with a mock task to avoid importing the real celery worker
        fake_module = ModuleType("app.workers.tasks.workspace_run")
        fake_task = MagicMock()
        fake_task.delay = MagicMock()
        fake_module.workspace_run_task = fake_task

        import sys

        sys.modules["app.workers.tasks.workspace_run"] = fake_module
        try:
            resp = await client.post(
                f"/api/v1/changesets/{changeset.id}/validate",
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            del sys.modules["app.workers.tasks.workspace_run"]

        assert resp.status_code == 202
        data = resp.json()
        assert data["run_type"] == "sdf_validate"
        assert data["status"] == "queued"

        # Verify audit event was emitted
        from app.models.audit import AuditEvent

        result = await db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "workspace.run.triggered",
                AuditEvent.resource_id == data["id"],
            )
        )
        audit = result.scalar_one_or_none()
        assert audit is not None

    @pytest.mark.asyncio
    async def test_trigger_validate_rejects_unapproved_changeset(self, db, tenant, user, changeset, client):
        from app.core.security import create_access_token

        token = create_access_token({"sub": str(user.id), "tenant_id": str(tenant.id)})
        resp = await client.post(
            f"/api/v1/changesets/{changeset.id}/validate",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400
        assert "approved" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_execute_run_emits_lifecycle_audits(self, db, tenant, user, workspace):
        from app.models.audit import AuditEvent

        run = await runner_service.create_run(db, tenant.id, workspace.id, "sdf_validate", user.id)
        await db.flush()

        with patch.object(
            runner_service,
            "_run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, "ok", ""),
        ):
            await runner_service.execute_run(db, run.id, tenant.id)

        events = (
            (
                await db.execute(
                    select(AuditEvent).where(
                        AuditEvent.resource_type.in_(["workspace_run", "workspace_artifact"]),
                        AuditEvent.correlation_id == run.correlation_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        actions = {e.action for e in events}
        assert "run_started" in actions
        assert "run_succeeded" in actions
        assert "artifact_created" in actions


# --- TestChangesetAssociation ---


class TestChangesetAssociation:
    @pytest.mark.asyncio
    async def test_run_links_to_changeset(self, db, tenant, user, workspace, changeset):
        run = await runner_service.create_run(
            db, tenant.id, workspace.id, "sdf_validate", user.id, changeset_id=changeset.id
        )
        assert run.changeset_id == changeset.id

    @pytest.mark.asyncio
    async def test_run_without_changeset(self, db, tenant, user, workspace):
        run = await runner_service.create_run(db, tenant.id, workspace.id, "sdf_validate", user.id)
        assert run.changeset_id is None


# --- TestTimeoutEnforcement ---


class TestTimeoutEnforcement:
    @pytest.mark.asyncio
    async def test_timeout_sets_error_status(self, db, tenant, user, workspace):
        run = await runner_service.create_run(db, tenant.id, workspace.id, "sdf_validate", user.id)
        await db.flush()

        import asyncio

        with patch.object(
            runner_service,
            "_run_subprocess",
            new_callable=AsyncMock,
            side_effect=asyncio.TimeoutError(),
        ):
            result = await runner_service.execute_run(db, run.id, tenant.id)

        assert result.status == "error"

        artifacts = await runner_service.get_artifacts(db, run.id, tenant.id)
        stderr_artifact = next((a for a in artifacts if a.artifact_type == "stderr"), None)
        assert stderr_artifact is not None
        assert "timed out" in stderr_artifact.content


# --- TestMcpTools ---


class TestMcpTools:
    def test_tools_registered(self):
        from app.mcp.registry import TOOL_REGISTRY

        assert "workspace.run_validate" in TOOL_REGISTRY
        assert "workspace.run_unit_tests" in TOOL_REGISTRY

    def test_governance_configs_exist(self):
        from app.mcp.governance import TOOL_CONFIGS

        assert "workspace.run_validate" in TOOL_CONFIGS
        assert "workspace.run_unit_tests" in TOOL_CONFIGS
        assert TOOL_CONFIGS["workspace.run_validate"]["rate_limit_per_minute"] == 5


# --- TestIdempotency ---


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_two_validate_runs_create_separate_records(self, db, tenant, user, workspace):
        run1 = await runner_service.create_run(db, tenant.id, workspace.id, "sdf_validate", user.id)
        run2 = await runner_service.create_run(db, tenant.id, workspace.id, "sdf_validate", user.id)
        assert run1.id != run2.id

        runs = await runner_service.list_runs(db, workspace.id, tenant.id)
        run_ids = {r.id for r in runs}
        assert run1.id in run_ids
        assert run2.id in run_ids
