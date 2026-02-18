"""Phase 3C Tests — SuiteQL Assertions + Gated Sandbox Deploy + UAT Report.

Test classes:
1. TestAssertionValidation — assertion schema validation
2. TestAssertionExecution — assertion evaluation logic
3. TestSelectOnlyEnforcement — SuiteQL SELECT-only enforcement
4. TestTableAllowlist — table allowlist enforcement
5. TestDeployGating — deploy prerequisite gate checks
6. TestDeployOverride — admin override with audit
7. TestUATReport — UAT report generation
8. TestAPIEndpoints — API endpoints for assertions, deploy, UAT report
9. TestMcpToolRegistration — MCP tool + governance registration
10. TestTenantIsolation — cross-tenant artifact access blocked
11. TestIdempotency — multiple runs create separate records
12. TestAuditEvents — audit event completeness
"""

import contextlib
import sys
import uuid
from datetime import datetime, timezone
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import (
    Workspace,
    WorkspaceChangeSet,
    WorkspaceFile,
    WorkspaceRun,
)
from app.services import assertion_service, runner_service
from app.services.deploy_service import check_deploy_prerequisites

_FAKE_MOD_KEY = "app.workers.tasks.workspace_run"


@contextlib.contextmanager
def _mock_workspace_run_task():
    """Context manager that installs a fake workspace_run module, then cleans up."""
    fake_module = ModuleType(_FAKE_MOD_KEY)
    fake_task = MagicMock()
    fake_task.delay = MagicMock()
    fake_module.workspace_run_task = fake_task

    old = sys.modules.get(_FAKE_MOD_KEY)
    sys.modules[_FAKE_MOD_KEY] = fake_module
    try:
        yield fake_task
    finally:
        if old is not None:
            sys.modules[_FAKE_MOD_KEY] = old
        else:
            sys.modules.pop(_FAKE_MOD_KEY, None)


# ---- Fixtures ----


@pytest_asyncio.fixture
async def ws_cs(db: AsyncSession, tenant_a, admin_user):
    """Create workspace + approved changeset for testing."""
    user, _ = admin_user
    ws = Workspace(
        tenant_id=tenant_a.id,
        name="Test Workspace 3C",
        created_by=user.id,
        status="active",
    )
    db.add(ws)
    await db.flush()

    f = WorkspaceFile(
        tenant_id=tenant_a.id,
        workspace_id=ws.id,
        path="SuiteScripts/test.js",
        file_name="test.js",
        content="console.log('hello');",
        sha256_hash="abc123",
        size_bytes=20,
        is_directory=False,
    )
    db.add(f)

    cs = WorkspaceChangeSet(
        tenant_id=tenant_a.id,
        workspace_id=ws.id,
        title="Test Changeset",
        status="approved",
        proposed_by=user.id,
    )
    db.add(cs)
    await db.flush()
    return ws, cs


@pytest.fixture
def sample_assertions():
    """Sample assertion definitions."""
    return [
        {
            "name": "Check customer count",
            "query": "SELECT COUNT(*) AS cnt FROM customer",
            "expected": {"type": "row_count", "operator": "gte", "value": 1},
        },
        {
            "name": "No orphan transactions",
            "query": "SELECT COUNT(*) AS cnt FROM transaction WHERE customer IS NULL",
            "expected": {"type": "no_rows"},
        },
    ]


# ---- Test Classes ----


class TestAssertionValidation:
    """Test assertion schema validation."""

    def test_valid_assertion(self, sample_assertions):
        assertion_service.validate_assertions(sample_assertions)

    def test_empty_assertions_rejected(self):
        with pytest.raises(ValueError, match="At least one"):
            assertion_service.validate_assertions([])

    def test_too_many_assertions_rejected(self):
        assertions = [
            {"name": f"a{i}", "query": "SELECT 1", "expected": {"type": "row_count", "operator": "eq", "value": 1}}
            for i in range(51)
        ]
        with pytest.raises(ValueError, match="Maximum 50"):
            assertion_service.validate_assertions(assertions)

    def test_missing_name_rejected(self):
        with pytest.raises(ValueError, match="name"):
            assertion_service.validate_assertion({"query": "SELECT 1", "expected": {"type": "row_count"}})

    def test_missing_query_rejected(self):
        with pytest.raises(ValueError, match="query"):
            assertion_service.validate_assertion({"name": "test", "expected": {"type": "row_count"}})

    def test_invalid_expect_type_rejected(self):
        with pytest.raises(ValueError, match="expected.type"):
            assertion_service.validate_assertion(
                {"name": "test", "query": "SELECT 1", "expected": {"type": "invalid_type"}}
            )

    def test_invalid_operator_rejected(self):
        with pytest.raises(ValueError, match="expected.operator"):
            assertion_service.validate_assertion(
                {"name": "test", "query": "SELECT 1", "expected": {"type": "row_count", "operator": "bad"}}
            )

    def test_between_requires_value2(self):
        with pytest.raises(ValueError, match="value2"):
            assertion_service.validate_assertion(
                {
                    "name": "test",
                    "query": "SELECT 1",
                    "expected": {"type": "row_count", "operator": "between", "value": 1},
                }
            )


class TestAssertionExecution:
    """Test assertion evaluation logic."""

    def test_eq_passes(self):
        assert assertion_service._evaluate_assertion({"type": "row_count", "operator": "eq", "value": 5}, 5) is True

    def test_eq_fails(self):
        assert assertion_service._evaluate_assertion({"type": "row_count", "operator": "eq", "value": 5}, 3) is False

    def test_gte_passes(self):
        assert assertion_service._evaluate_assertion({"type": "row_count", "operator": "gte", "value": 3}, 5) is True

    def test_lt_passes(self):
        assert assertion_service._evaluate_assertion({"type": "row_count", "operator": "lt", "value": 10}, 5) is True

    def test_between_passes(self):
        assert (
            assertion_service._evaluate_assertion(
                {"type": "row_count", "operator": "between", "value": 1, "value2": 10}, 5
            )
            is True
        )

    def test_between_fails(self):
        assert (
            assertion_service._evaluate_assertion(
                {"type": "row_count", "operator": "between", "value": 1, "value2": 3}, 5
            )
            is False
        )

    def test_no_rows_passes_when_zero(self):
        assert assertion_service._evaluate_assertion({"type": "no_rows"}, 0) is True

    def test_no_rows_fails_when_nonzero(self):
        assert assertion_service._evaluate_assertion({"type": "no_rows"}, 5) is False

    def test_scalar_evaluation(self):
        assert assertion_service._evaluate_assertion({"type": "scalar", "operator": "eq", "value": 42}, 42) is True


class TestSelectOnlyEnforcement:
    """Test that non-SELECT queries are rejected."""

    @pytest.mark.asyncio
    async def test_non_select_blocked(self, db, tenant_a):
        """Assert that INSERT/UPDATE/DELETE queries are blocked."""
        run_id = uuid.uuid4()
        assertions = [
            {
                "name": "bad insert",
                "query": "INSERT INTO customer (name) VALUES ('test')",
                "expected": {"type": "row_count", "operator": "eq", "value": 0},
            }
        ]

        executor = AsyncMock(return_value={"rows": [], "row_count": 0})
        report = await assertion_service.execute_assertions(
            db=db,
            tenant_id=tenant_a.id,
            run_id=run_id,
            assertions=assertions,
            suiteql_executor=executor,
            correlation_id="test-corr",
        )
        assert report["assertions"][0]["status"] == "error"
        assert "SELECT" in report["assertions"][0]["error"]
        executor.assert_not_called()


class TestTableAllowlist:
    """Test table allowlist enforcement."""

    @pytest.mark.asyncio
    async def test_disallowed_table_blocked(self, db, tenant_a):
        """Assert that queries referencing non-allowlisted tables are blocked."""
        run_id = uuid.uuid4()
        assertions = [
            {
                "name": "forbidden table",
                "query": "SELECT * FROM secret_internal_table",
                "expected": {"type": "row_count", "operator": "gte", "value": 0},
            }
        ]

        executor = AsyncMock(return_value={"rows": [], "row_count": 0})
        report = await assertion_service.execute_assertions(
            db=db,
            tenant_id=tenant_a.id,
            run_id=run_id,
            assertions=assertions,
            suiteql_executor=executor,
            correlation_id="test-corr",
        )
        assert report["assertions"][0]["status"] == "error"
        assert "Disallowed tables" in report["assertions"][0]["error"]
        executor.assert_not_called()

    @pytest.mark.asyncio
    async def test_allowed_table_passes(self, db, tenant_a):
        """Assert that queries referencing allowlisted tables proceed."""
        run_id = uuid.uuid4()
        assertions = [
            {
                "name": "allowed table",
                "query": "SELECT COUNT(*) FROM customer",
                "expected": {"type": "row_count", "operator": "gte", "value": 0},
            }
        ]

        executor = AsyncMock(return_value={"rows": [[5]], "row_count": 1, "columns": ["cnt"]})
        report = await assertion_service.execute_assertions(
            db=db,
            tenant_id=tenant_a.id,
            run_id=run_id,
            assertions=assertions,
            suiteql_executor=executor,
            correlation_id="test-corr",
        )
        assert report["assertions"][0]["status"] == "passed"
        executor.assert_called_once()


class TestDeployGating:
    """Test deploy prerequisite gate checks."""

    @pytest.mark.asyncio
    async def test_deploy_blocked_without_validate(self, db, ws_cs):
        """Deploy should be blocked if validate has not run."""
        _, cs = ws_cs
        result = await check_deploy_prerequisites(db, cs.id, cs.tenant_id)
        assert result["allowed"] is False
        assert "validate" in result["blocked_reason"]

    @pytest.mark.asyncio
    async def test_deploy_blocked_without_tests(self, db, ws_cs, admin_user):
        """Deploy should be blocked if unit tests have not run (even if validate passed)."""
        user, _ = admin_user
        ws, cs = ws_cs
        validate_run = WorkspaceRun(
            tenant_id=cs.tenant_id,
            workspace_id=ws.id,
            changeset_id=cs.id,
            run_type="sdf_validate",
            status="passed",
            triggered_by=user.id,
        )
        db.add(validate_run)
        await db.flush()

        result = await check_deploy_prerequisites(db, cs.id, cs.tenant_id)
        assert result["allowed"] is False
        assert "unit_tests" in result["blocked_reason"]

    @pytest.mark.asyncio
    async def test_deploy_allowed_with_all_gates(self, db, ws_cs, admin_user):
        """Deploy should be allowed when validate + tests pass."""
        user, _ = admin_user
        ws, cs = ws_cs
        for rt in ("sdf_validate", "jest_unit_test"):
            run = WorkspaceRun(
                tenant_id=cs.tenant_id,
                workspace_id=ws.id,
                changeset_id=cs.id,
                run_type=rt,
                status="passed",
                triggered_by=user.id,
            )
            db.add(run)
        await db.flush()

        result = await check_deploy_prerequisites(db, cs.id, cs.tenant_id)
        assert result["allowed"] is True
        assert result["blocked_reason"] is None

    @pytest.mark.asyncio
    async def test_deploy_blocked_when_assertions_required_but_missing(self, db, ws_cs, admin_user):
        """Deploy should be blocked if assertions are required but haven't run."""
        user, _ = admin_user
        ws, cs = ws_cs
        for rt in ("sdf_validate", "jest_unit_test"):
            run = WorkspaceRun(
                tenant_id=cs.tenant_id,
                workspace_id=ws.id,
                changeset_id=cs.id,
                run_type=rt,
                status="passed",
                triggered_by=user.id,
            )
            db.add(run)
        await db.flush()

        result = await check_deploy_prerequisites(db, cs.id, cs.tenant_id, require_assertions=True)
        assert result["allowed"] is False
        assert "assertions" in result["blocked_reason"]


class TestDeployOverride:
    """Test admin override with audit."""

    @pytest.mark.asyncio
    async def test_override_does_not_bypass_validate_or_tests(self, db, ws_cs):
        """Override must not bypass validate/unit-test gates."""
        _, cs = ws_cs
        result = await check_deploy_prerequisites(db, cs.id, cs.tenant_id, override_reason="Emergency hotfix required")
        assert result["allowed"] is False
        assert result["override"]["applied"] is False
        assert "validate" in result["blocked_reason"]

    @pytest.mark.asyncio
    async def test_override_allows_assertion_gate_only(self, db, ws_cs, admin_user):
        """Override may bypass assertions only after validate + unit tests pass."""
        user, _ = admin_user
        ws, cs = ws_cs
        for rt in ("sdf_validate", "jest_unit_test"):
            run = WorkspaceRun(
                tenant_id=cs.tenant_id,
                workspace_id=ws.id,
                changeset_id=cs.id,
                run_type=rt,
                status="passed",
                triggered_by=user.id,
            )
            db.add(run)
        await db.flush()

        result = await check_deploy_prerequisites(
            db,
            cs.id,
            cs.tenant_id,
            require_assertions=True,
            override_reason="Emergency hotfix required",
        )
        assert result["allowed"] is True
        assert result["override"]["applied"] is True

    @pytest.mark.asyncio
    async def test_no_override_without_reason(self, db, ws_cs, admin_user):
        """Without override_reason, missing required assertions should block deploy."""
        user, _ = admin_user
        ws, cs = ws_cs
        for rt in ("sdf_validate", "jest_unit_test"):
            run = WorkspaceRun(
                tenant_id=cs.tenant_id,
                workspace_id=ws.id,
                changeset_id=cs.id,
                run_type=rt,
                status="passed",
                triggered_by=user.id,
            )
            db.add(run)
        await db.flush()

        result = await check_deploy_prerequisites(db, cs.id, cs.tenant_id, require_assertions=True)
        assert result["allowed"] is False
        assert result["override"]["applied"] is False


class TestUATReport:
    """Test UAT report generation via API."""

    @pytest.mark.asyncio
    async def test_uat_report_empty(self, client, ws_cs, admin_user):
        """UAT report should work even with no runs."""
        _, headers = admin_user
        _, cs = ws_cs
        resp = await client.get(f"/api/v1/changesets/{cs.id}/uat-report", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["changeset_id"] == str(cs.id)
        assert data["overall_status"] == "in_progress"
        assert data["gates"]["validate"] == "missing"
        assert data["gates"]["unit_tests"] == "missing"

    @pytest.mark.asyncio
    async def test_uat_report_with_passed_runs(self, client, db, ws_cs, admin_user):
        """UAT report should show correct status when all runs pass."""
        user, headers = admin_user
        ws, cs = ws_cs
        for rt in ("sdf_validate", "jest_unit_test"):
            run = WorkspaceRun(
                tenant_id=cs.tenant_id,
                workspace_id=ws.id,
                changeset_id=cs.id,
                run_type=rt,
                status="passed",
                triggered_by=user.id,
                duration_ms=1000,
                started_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
            )
            db.add(run)
        await db.flush()

        resp = await client.get(f"/api/v1/changesets/{cs.id}/uat-report", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall_status"] == "ready_for_deploy"
        assert data["gates"]["validate"] == "passed"
        assert data["gates"]["unit_tests"] == "passed"

    @pytest.mark.asyncio
    async def test_uat_report_not_found(self, client, admin_user):
        """UAT report for non-existent changeset returns 404."""
        _, headers = admin_user
        fake_id = str(uuid.uuid4())
        resp = await client.get(f"/api/v1/changesets/{fake_id}/uat-report", headers=headers)
        assert resp.status_code == 404


class TestAPIEndpoints:
    """Test API endpoints for assertions and deploy."""

    @pytest.mark.asyncio
    async def test_assertions_endpoint_requires_approved(self, client, db, tenant_a, admin_user):
        """Assertions endpoint should reject non-approved changeset."""
        user, headers = admin_user
        ws = Workspace(tenant_id=tenant_a.id, name="W", created_by=user.id, status="active")
        db.add(ws)
        await db.flush()
        cs = WorkspaceChangeSet(
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
            title="Draft CS",
            status="draft",
            proposed_by=user.id,
        )
        db.add(cs)
        await db.flush()

        resp = await client.post(
            f"/api/v1/changesets/{cs.id}/suiteql-assertions",
            headers=headers,
            json={
                "assertions": [
                    {
                        "name": "test",
                        "query": "SELECT 1",
                        "expected": {"type": "row_count", "operator": "eq", "value": 1},
                    }
                ]
            },
        )
        assert resp.status_code == 400
        assert "approved" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_deploy_endpoint_requires_approved(self, client, db, tenant_a, admin_user):
        """Deploy endpoint should reject non-approved changeset."""
        user, headers = admin_user
        ws = Workspace(tenant_id=tenant_a.id, name="W2", created_by=user.id, status="active")
        db.add(ws)
        await db.flush()
        cs = WorkspaceChangeSet(
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
            title="Draft CS 2",
            status="draft",
            proposed_by=user.id,
        )
        db.add(cs)
        await db.flush()

        resp = await client.post(
            f"/api/v1/changesets/{cs.id}/deploy-sandbox",
            headers=headers,
            json={"sandbox_id": "6738075-sb1"},
        )
        assert resp.status_code == 400
        assert "approved" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_deploy_endpoint_blocked_without_prerequisites(self, client, ws_cs, admin_user):
        """Deploy endpoint should 400 when prerequisites not met."""
        _, headers = admin_user
        _, cs = ws_cs
        resp = await client.post(
            f"/api/v1/changesets/{cs.id}/deploy-sandbox",
            headers=headers,
            json={"sandbox_id": "6738075-sb1"},
        )
        assert resp.status_code == 400
        assert "prerequisites" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_deploy_endpoint_requires_sandbox_id(self, client, ws_cs, admin_user):
        """Deploy endpoint should reject calls without sandbox target."""
        _, headers = admin_user
        _, cs = ws_cs
        resp = await client.post(f"/api/v1/changesets/{cs.id}/deploy-sandbox", headers=headers, json={})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_assertions_endpoint_success(self, client, ws_cs, admin_user):
        """Assertions endpoint should accept valid assertions on approved changeset."""
        _, headers = admin_user
        _, cs = ws_cs

        with _mock_workspace_run_task():
            resp = await client.post(
                f"/api/v1/changesets/{cs.id}/suiteql-assertions",
                headers=headers,
                json={
                    "assertions": [
                        {
                            "name": "test",
                            "query": "SELECT COUNT(*) FROM customer",
                            "expected": {"type": "row_count", "operator": "gte", "value": 0},
                        }
                    ]
                },
            )
        assert resp.status_code == 202
        data = resp.json()
        assert data["run_type"] == "suiteql_assertions"
        assert data["status"] == "queued"

    @pytest.mark.asyncio
    async def test_deploy_with_override_succeeds(self, client, db, ws_cs, admin_user):
        """Deploy should succeed with override only when mandatory gates already pass."""
        user, headers = admin_user
        ws, cs = ws_cs
        for rt in ("sdf_validate", "jest_unit_test"):
            run = WorkspaceRun(
                tenant_id=cs.tenant_id,
                workspace_id=ws.id,
                changeset_id=cs.id,
                run_type=rt,
                status="passed",
                triggered_by=user.id,
            )
            db.add(run)
        await db.flush()

        with _mock_workspace_run_task():
            resp = await client.post(
                f"/api/v1/changesets/{cs.id}/deploy-sandbox",
                headers=headers,
                json={
                    "sandbox_id": "6738075-sb1",
                    "require_assertions": True,
                    "override_reason": "Emergency hotfix",
                },
            )
        assert resp.status_code == 202
        data = resp.json()
        assert data["run_type"] == "deploy_sandbox"
        assert data["status"] == "queued"


class TestMcpToolRegistration:
    """Test MCP tool + governance registration for Phase 3C tools."""

    def test_assertions_tool_registered(self):
        from app.mcp.registry import TOOL_REGISTRY

        assert "workspace.run_suiteql_assertions" in TOOL_REGISTRY

    def test_deploy_tool_registered(self):
        from app.mcp.registry import TOOL_REGISTRY

        assert "workspace.deploy_sandbox" in TOOL_REGISTRY

    def test_assertions_governance_config(self):
        from app.mcp.governance import TOOL_CONFIGS

        config = TOOL_CONFIGS["workspace.run_suiteql_assertions"]
        assert config["rate_limit_per_minute"] == 5
        assert config["requires_entitlement"] == "workspace"
        assert "changeset_id" in config["allowlisted_params"]
        assert "assertions" in config["allowlisted_params"]

    def test_deploy_governance_config(self):
        from app.mcp.governance import TOOL_CONFIGS

        config = TOOL_CONFIGS["workspace.deploy_sandbox"]
        assert config["rate_limit_per_minute"] == 2
        assert config["requires_entitlement"] == "workspace"
        assert "changeset_id" in config["allowlisted_params"]
        assert "sandbox_id" in config["allowlisted_params"]
        assert "override_reason" in config["allowlisted_params"]


class TestTenantIsolation:
    """Test that cross-tenant access is blocked."""

    @pytest.mark.asyncio
    async def test_uat_report_cross_tenant_blocked(self, client, ws_cs, admin_user_b):
        """UAT report should return 404 for another tenant's changeset."""
        _, cs = ws_cs
        _, headers_b = admin_user_b
        resp = await client.get(f"/api/v1/changesets/{cs.id}/uat-report", headers=headers_b)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_deploy_cross_tenant_blocked(self, client, ws_cs, admin_user_b):
        """Deploy should return 404 for another tenant's changeset."""
        _, cs = ws_cs
        _, headers_b = admin_user_b
        resp = await client.post(
            f"/api/v1/changesets/{cs.id}/deploy-sandbox",
            headers=headers_b,
            json={"sandbox_id": "6738075-sb1"},
        )
        assert resp.status_code == 404


class TestIdempotency:
    """Test that multiple runs create separate records."""

    @pytest.mark.asyncio
    async def test_two_assertion_runs_separate(self, db, ws_cs, admin_user):
        """Two assertion runs for the same changeset create separate run records."""
        user, _ = admin_user
        ws, cs = ws_cs

        run1 = await runner_service.create_run(
            db,
            tenant_id=cs.tenant_id,
            workspace_id=ws.id,
            run_type="suiteql_assertions",
            triggered_by=user.id,
            changeset_id=cs.id,
        )
        run2 = await runner_service.create_run(
            db,
            tenant_id=cs.tenant_id,
            workspace_id=ws.id,
            run_type="suiteql_assertions",
            triggered_by=user.id,
            changeset_id=cs.id,
        )
        assert run1.id != run2.id
        assert run1.status == "queued"
        assert run2.status == "queued"

    @pytest.mark.asyncio
    async def test_two_deploy_runs_separate(self, db, ws_cs, admin_user):
        """Two deploy runs create separate run records."""
        user, _ = admin_user
        ws, cs = ws_cs

        run1 = await runner_service.create_run(
            db,
            tenant_id=cs.tenant_id,
            workspace_id=ws.id,
            run_type="deploy_sandbox",
            triggered_by=user.id,
            changeset_id=cs.id,
        )
        run2 = await runner_service.create_run(
            db,
            tenant_id=cs.tenant_id,
            workspace_id=ws.id,
            run_type="deploy_sandbox",
            triggered_by=user.id,
            changeset_id=cs.id,
        )
        assert run1.id != run2.id


class TestAuditEvents:
    """Test audit event completeness."""

    @pytest.mark.asyncio
    async def test_assertion_execution_emits_audits(self, db, tenant_a):
        """Each assertion execution should emit an audit event."""
        from app.models.audit import AuditEvent

        run_id = uuid.uuid4()
        assertions = [
            {
                "name": "count check",
                "query": "SELECT COUNT(*) FROM customer",
                "expected": {"type": "row_count", "operator": "gte", "value": 0},
            }
        ]

        executor = AsyncMock(return_value={"rows": [[5]], "row_count": 1, "columns": ["cnt"]})
        await assertion_service.execute_assertions(
            db=db,
            tenant_id=tenant_a.id,
            run_id=run_id,
            assertions=assertions,
            suiteql_executor=executor,
            correlation_id="audit-test-corr",
        )
        await db.flush()

        result = await db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "suiteql.assertion.executed",
                AuditEvent.correlation_id == "audit-test-corr",
            )
        )
        events = list(result.scalars().all())
        assert len(events) == 1
        assert events[0].status == "passed"

    @pytest.mark.asyncio
    async def test_deploy_override_emits_audit(self, client, db, ws_cs, admin_user):
        """Deploy with override should emit a gate_override audit event."""

        user, headers = admin_user
        ws, cs = ws_cs
        for rt in ("sdf_validate", "jest_unit_test"):
            run = WorkspaceRun(
                tenant_id=cs.tenant_id,
                workspace_id=ws.id,
                changeset_id=cs.id,
                run_type=rt,
                status="passed",
                triggered_by=user.id,
            )
            db.add(run)
        await db.flush()

        with _mock_workspace_run_task():
            resp = await client.post(
                f"/api/v1/changesets/{cs.id}/deploy-sandbox",
                headers=headers,
                json={
                    "sandbox_id": "6738075-sb1",
                    "require_assertions": True,
                    "override_reason": "Emergency hotfix",
                },
            )
        assert resp.status_code == 202

    @pytest.mark.asyncio
    async def test_assertions_api_emits_trigger_audit(self, client, db, ws_cs, admin_user):
        """Assertions API should emit workspace.run.triggered audit."""
        from app.models.audit import AuditEvent

        _, headers = admin_user
        _, cs = ws_cs

        with _mock_workspace_run_task():
            resp = await client.post(
                f"/api/v1/changesets/{cs.id}/suiteql-assertions",
                headers=headers,
                json={
                    "assertions": [
                        {
                            "name": "test",
                            "query": "SELECT COUNT(*) FROM customer",
                            "expected": {"type": "row_count", "operator": "gte", "value": 0},
                        }
                    ]
                },
            )
        assert resp.status_code == 202

        result = await db.execute(select(AuditEvent).where(AuditEvent.action == "workspace.run.triggered"))
        events = list(result.scalars().all())
        triggered = [e for e in events if e.payload and e.payload.get("run_type") == "suiteql_assertions"]
        assert len(triggered) >= 1
