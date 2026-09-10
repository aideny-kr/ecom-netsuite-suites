"""Tests for MCP tool governance: rate limiting, param validation, redaction, audit."""

import uuid

import pytest

from app.mcp.governance import (
    TOOL_CONFIGS,
    check_rate_limit,
    create_audit_payload,
    governed_execute,
    redact_result,
    reset_rate_limit,
    validate_params,
)


class TestParamValidation:
    def test_filters_to_allowlist(self):
        result = validate_params(
            "netsuite.suiteql",
            {
                "query": "SELECT * FROM transaction",
                "limit": 50,
                "evil_param": "DROP TABLE",
            },
        )
        assert "query" in result
        assert "limit" in result
        assert "evil_param" not in result

    def test_injects_default_limit(self):
        result = validate_params(
            "netsuite.suiteql",
            {
                "query": "SELECT * FROM transaction",
            },
        )
        assert result["limit"] == 100  # default_limit

    def test_caps_at_max_limit(self):
        # max_limit now tracks settings.NETSUITE_SUITEQL_MAX_ROWS (50000).
        # Anything above that should clamp; under it passes through.
        result = validate_params(
            "netsuite.suiteql",
            {
                "query": "SELECT * FROM transaction",
                "limit": 100000,
            },
        )
        assert result["limit"] == 50000  # max_limit (== settings.NETSUITE_SUITEQL_MAX_ROWS)

    def test_no_allowlist_passes_all(self):
        # schedule.list has empty allowlisted_params
        result = validate_params("schedule.list", {"extra": "value"})
        assert "extra" in result

    def test_schedule_create_allowlist_lets_every_registry_field_through(self):
        """Item 6 (gate fix): the OLD allowlist (name, schedule_type, cron,
        params) stripped instruction/timezone/delivery before execute_create
        ever saw them — a chat-created Scheduled Job silently fell through
        to the legacy path. Every field the registry's own params_schema
        declares for schedule.create must survive validate_params."""
        result = validate_params(
            "schedule.create",
            {
                "instruction": "weekly inventory aging report",
                "name": "Inventory Aging Weekly",
                "schedule_type": "job",
                "cron": "0 6 * * 1",
                "timezone": "America/Los_Angeles",
                "delivery": {"drive": True},
                "params": {"a": 1},
                "evil_param": "DROP TABLE",
            },
        )
        assert result == {
            "instruction": "weekly inventory aging report",
            "name": "Inventory Aging Weekly",
            "schedule_type": "job",
            "cron": "0 6 * * 1",
            "timezone": "America/Los_Angeles",
            "delivery": {"drive": True},
            "params": {"a": 1},
        }

    def test_schedule_run_allowlist_lets_use_pending_through(self):
        """Item 6 (gate fix): the OLD allowlist (schedule_id only) stripped
        use_pending before execute_run ever saw it."""
        result = validate_params(
            "schedule.run",
            {"schedule_id": "abc-123", "use_pending": True, "evil_param": "DROP TABLE"},
        )
        assert result == {"schedule_id": "abc-123", "use_pending": True}


class TestRateLimiting:
    def setup_method(self):
        """Clear rate limit state between tests."""
        reset_rate_limit()

    def test_within_limit(self):
        tenant = str(uuid.uuid4())
        for _ in range(10):
            assert check_rate_limit(tenant, "netsuite.suiteql") is True

    def test_exceeds_limit(self):
        tenant = str(uuid.uuid4())
        tool = "netsuite.suiteql"
        limit = TOOL_CONFIGS[tool]["rate_limit_per_minute"]

        # Fill up the limit
        for _ in range(limit):
            assert check_rate_limit(tenant, tool) is True

        # Next one should be denied
        assert check_rate_limit(tenant, tool) is False

    def test_different_tenants_separate_limits(self):
        tenant_a = str(uuid.uuid4())
        tenant_b = str(uuid.uuid4())
        tool = "recon.run"
        limit = TOOL_CONFIGS[tool]["rate_limit_per_minute"]

        for _ in range(limit):
            check_rate_limit(tenant_a, tool)

        # Tenant B should still be allowed
        assert check_rate_limit(tenant_b, tool) is True


class TestResultRedaction:
    def test_redacts_sensitive_keys(self):
        result = redact_result(
            {
                "data": "safe",
                "token": "sk_live_secret",
                "api_key": "key123",
                "password": "pass123",
            }
        )
        assert result["data"] == "safe"
        assert result["token"] == "***REDACTED***"
        assert result["api_key"] == "***REDACTED***"
        assert result["password"] == "***REDACTED***"

    def test_redacts_nested(self):
        result = redact_result(
            {
                "config": {
                    "token": "nested_secret",
                    "name": "safe",
                }
            }
        )
        assert result["config"]["token"] == "***REDACTED***"
        assert result["config"]["name"] == "safe"

    def test_no_sensitive_keys(self):
        result = redact_result({"rows": [1, 2, 3], "count": 3})
        assert result == {"rows": [1, 2, 3], "count": 3}


class TestAuditPayload:
    def test_creates_payload(self):
        payload = create_audit_payload(
            "netsuite.suiteql",
            {"query": "SELECT * FROM items", "limit": 100},
            result={"row_count": 5},
        )
        assert payload["tool_name"] == "netsuite.suiteql"
        assert payload["params"]["query"] == "SELECT * FROM items"
        assert payload["result_summary"]["status"] == "success"
        assert payload["result_summary"]["row_count"] == 5

    def test_scrubs_sensitive_params(self):
        payload = create_audit_payload(
            "netsuite.suiteql",
            {
                "query": "SELECT *",
                "password": "secret123",
                "token": "abc",
                "api_key": "sk-live-xxx",
                "credentials": {"key": "val"},
            },
        )
        assert "password" not in payload["params"]
        assert "token" not in payload["params"]
        assert "api_key" not in payload["params"]
        assert "credentials" not in payload["params"]
        assert payload["params"]["query"] == "SELECT *"

    def test_error_payload(self):
        payload = create_audit_payload(
            "netsuite.suiteql",
            {"query": "BAD SQL"},
            error="Syntax error",
        )
        assert payload["result_summary"]["status"] == "error"
        assert payload["result_summary"]["error"] == "Syntax error"


class TestGovernedExecute:
    async def test_successful_execution(self):
        reset_rate_limit()

        async def stub_fn(params, **kwargs):
            return {"status": "stub", "row_count": 0, "data": []}

        result = await governed_execute(
            tool_name="netsuite.suiteql",
            params={"query": "SELECT * FROM items"},
            tenant_id=str(uuid.uuid4()),
            actor_id=str(uuid.uuid4()),
            execute_fn=stub_fn,
        )
        assert "error" not in result
        assert result["status"] == "stub"

    async def test_rate_limited_execution(self):
        reset_rate_limit()
        tenant_id = str(uuid.uuid4())
        tool = "recon.run"
        limit = TOOL_CONFIGS[tool]["rate_limit_per_minute"]

        async def stub_fn(params, **kwargs):
            return {"status": "ok"}

        # Exhaust rate limit
        for _ in range(limit):
            await governed_execute(tool, {}, tenant_id, None, stub_fn)

        # Next call should be rate limited
        result = await governed_execute(tool, {}, tenant_id, None, stub_fn)
        assert "error" in result
        assert "rate limit" in result["error"].lower()

    async def test_execution_error_handled(self):
        reset_rate_limit()

        async def failing_fn(params, **kwargs):
            raise ValueError("Tool broke")

        # Use a non-SuiteQL tool to avoid pre-execution validation intercepting the call
        result = await governed_execute(
            "recon.run",
            {"date_from": "2026-01-01"},
            str(uuid.uuid4()),
            None,
            failing_fn,
        )
        assert "error" in result
        assert "Tool broke" in result["error"]


class TestGovernedExecuteReestablishesTenantContext:
    """Item 5 (delta gate fix E): the "re-set tenant context after commit"
    block used to be pasted into each MCP handler that commits mid-call
    (schedule_ops.py's execute_create/execute_run, each carrying its own
    identical comment) -- moved to the ONE choke point every tool call
    passes through, `governed_execute`, immediately after `execute_fn`
    returns (success or error path). A REAL commit is needed to exercise
    this (the shared test `db` fixture's own commit is a RELEASE SAVEPOINT,
    which does NOT clear `SET LOCAL` GUCs -- see
    tests/test_schedule_ops_tool.py's own `_spy_commit_then_ctx` docstring),
    so this opens its own engine/session exactly like
    tests/jobs/test_executor.py::test_skip_locked_prevents_double_run does,
    skipping the same way against a non-local database."""

    async def test_a_handler_that_commits_leaves_tenant_context_set_afterward(self):
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

        from app.core.config import settings
        from app.core.database import set_tenant_context
        from tests.conftest import create_test_tenant

        reset_rate_limit()
        db_url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
        if "supabase" in db_url:
            pytest.skip("real-commit tenant-context test runs against LOCAL docker only")

        engine = create_async_engine(db_url, echo=False)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                tenant = await create_test_tenant(
                    db, name="GovernedExecuteCtxCo", slug=f"gov-ctx-{uuid.uuid4().hex[:8]}"
                )
                await db.commit()
                tenant_id = tenant.id
                await set_tenant_context(db, str(tenant_id))

                async def commits_mid_call(params, **kwargs):
                    ctx = kwargs["context"]
                    inner_db = ctx["db"]
                    # Mirrors a real handler (schedule_ops.py's execute_create/
                    # execute_run): a REAL commit mid-call clears SET LOCAL.
                    await inner_db.commit()
                    return {"status": "ok"}

                result = await governed_execute(
                    tool_name="schedule.run",
                    params={"schedule_id": str(uuid.uuid4())},
                    tenant_id=str(tenant_id),
                    actor_id=None,
                    execute_fn=commits_mid_call,
                    db=db,
                )
                assert "error" not in result

                try:
                    row = (await db.execute(text("SELECT current_setting('app.current_tenant_id', true)"))).scalar_one()
                except Exception as exc:
                    pytest.fail(f"tenant context was not usable after governed_execute: {exc}")
                assert row == str(tenant_id)
        finally:
            await engine.dispose()


class TestToolConfigs:
    """Verify all expected tools are configured."""

    def test_all_tools_present(self):
        expected = {
            "health",
            "netsuite.suiteql",
            "netsuite.suiteql_stub",
            "netsuite.connectivity",
            "data.sample_table_read",
            "recon.run",
            "recon.approve_group",
            "rag.search",
            "web.search",
            "schedule.create",
            "schedule.list",
            "schedule.run",
            "workspace.list_files",
            "workspace.read_file",
            "workspace.search",
            "workspace.propose_patch",
            "workspace.apply_patch",
            "workspace.run_validate",
            "workspace.run_unit_tests",
            "workspace.deploy_sandbox",
            "workspace.deploy_sandbox_confirm",
            "workspace.run_suiteql_assertions",
            "suitescript.sync",
            "bigquery.sql",
            "bigquery.schema",
            "bigquery.cost_estimate",
            "sheets.create",
            "sheets.write_range",
            "sheets.read_range",
            "report.compose",
            "celigo.integrations",
            "celigo.flows",
            "celigo.flow_steps",
            "celigo.flow_errors",
        }
        assert set(TOOL_CONFIGS.keys()) == expected

    def test_all_have_required_fields(self):
        for name, config in TOOL_CONFIGS.items():
            assert "timeout_seconds" in config, f"{name} missing timeout_seconds"
            assert "rate_limit_per_minute" in config, f"{name} missing rate_limit_per_minute"
            assert "requires_entitlement" in config, f"{name} missing requires_entitlement"
            assert "allowlisted_params" in config, f"{name} missing allowlisted_params"

    def test_schedule_tool_allowlists_never_drift_from_the_registry_params_schema(self):
        """Item 6 (gate fix): governed_execute -> validate_params filters
        params to `allowlisted_params` BEFORE the tool's own execute() ever
        sees them — a param the registry's params_schema declares but this
        allowlist omits silently vanishes rather than raising anywhere.
        Scoped to schedule.* deliberately (not every TOOL_CONFIGS entry):
        two pre-existing tools already violate this same invariant —
        netsuite.suiteql (registry declares `user_question`, not in the
        allowlist — a prompt-guidance-only field, intentionally stripped)
        and workspace.list_files (allowlist carries a `limit` the registry
        schema doesn't declare) — fixing those is a separate, wider change
        this item does not scope to."""
        from app.mcp.registry import TOOL_REGISTRY

        for name in ("schedule.create", "schedule.list", "schedule.run"):
            allow = set(TOOL_CONFIGS[name].get("allowlisted_params") or [])
            if not allow:
                continue
            schema_keys = set((TOOL_REGISTRY[name].get("params_schema") or {}).keys())
            assert allow == schema_keys, f"{name}: allowlist={sorted(allow)} vs registry={sorted(schema_keys)}"
