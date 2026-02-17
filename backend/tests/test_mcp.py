"""Tests for MCP tool governance: rate limiting, param validation, redaction, audit."""

import uuid

from app.mcp.governance import (
    TOOL_CONFIGS,
    _rate_limits,
    check_rate_limit,
    create_audit_payload,
    governed_execute,
    redact_result,
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
        result = validate_params(
            "netsuite.suiteql",
            {
                "query": "SELECT * FROM transaction",
                "limit": 5000,
            },
        )
        assert result["limit"] == 1000  # max_limit

    def test_no_allowlist_passes_all(self):
        # schedule.list has empty allowlisted_params
        result = validate_params("schedule.list", {"extra": "value"})
        assert "extra" in result


class TestRateLimiting:
    def setup_method(self):
        """Clear rate limit state between tests."""
        _rate_limits.clear()

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
        _rate_limits.clear()

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
        _rate_limits.clear()
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
        _rate_limits.clear()

        async def failing_fn(params, **kwargs):
            raise ValueError("Tool broke")

        result = await governed_execute(
            "netsuite.suiteql",
            {"query": "SELECT *"},
            str(uuid.uuid4()),
            None,
            failing_fn,
        )
        assert "error" in result
        assert "Tool broke" in result["error"]


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
            "report.export",
            "schedule.create",
            "schedule.list",
            "schedule.run",
            "workspace.list_files",
            "workspace.read_file",
            "workspace.search",
            "workspace.propose_patch",
            "workspace.apply_patch",
        }
        assert set(TOOL_CONFIGS.keys()) == expected

    def test_all_have_required_fields(self):
        for name, config in TOOL_CONFIGS.items():
            assert "timeout_seconds" in config, f"{name} missing timeout_seconds"
            assert "rate_limit_per_minute" in config, f"{name} missing rate_limit_per_minute"
            assert "requires_entitlement" in config, f"{name} missing requires_entitlement"
            assert "allowlisted_params" in config, f"{name} missing allowlisted_params"
