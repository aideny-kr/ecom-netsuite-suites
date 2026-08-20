"""Celigo read-only policy — the single source of truth for both guard layers."""

import pytest

from app.services.chat.celigo_tool_policy import (
    CELIGO_WRITE_VERBS,
    is_celigo_provider,
    is_read_only_celigo_tool,
)


class TestProviderDetection:
    def test_celigo_mcp_is_celigo(self):
        assert is_celigo_provider("celigo_mcp") is True

    def test_netsuite_is_not_celigo(self):
        assert is_celigo_provider("netsuite_mcp") is False

    def test_none_is_not_celigo(self):
        assert is_celigo_provider("") is False


class TestReadOnlyAllowlist:
    @pytest.mark.parametrize(
        "tool",
        [
            "list_flows",
            "list_integrations",
            "list_scripts",
            "list_exports",
            "list_imports",
            "list_connections",
            "list_flow_errors",
            "get_schema",
            "search_knowledge_base",
        ],
    )
    def test_read_tools_allowed(self, tool):
        assert is_read_only_celigo_tool(tool) is True

    @pytest.mark.parametrize(
        "tool",
        [
            "upsert_flow",
            "upsert_connection",
            "upsert_script",
            "patch_flow",
            "run_flow",
            "delete_resource",
            "delete_lookup_cache_data",
            "deploy_template",
            "manage_user",
            "cancel_job",
            "cancel_storage_upload",
            "restore_storage_item",
            "triage_flow_errors",
            "update_edi_fa_status",
            "update_flow_error_retry_data",
        ],
    )
    def test_write_tools_denied(self, tool):
        assert is_read_only_celigo_tool(tool) is False

    def test_unknown_tool_fails_closed(self):
        """A Celigo tool we have never seen is denied until reviewed.

        This is the opposite trade-off from _BLOCKED_RECORD_TYPES (agent-graph.md #1),
        and deliberately so: the Celigo tool surface is enumerable and read-only, so
        failing closed on an unknown name is correct.
        """
        assert is_read_only_celigo_tool("some_future_celigo_tool") is False
        assert is_read_only_celigo_tool("") is False

    def test_prefix_match_is_not_substring_match(self):
        """'list_' must anchor at the start — a write tool must not sneak through."""
        assert is_read_only_celigo_tool("delete_list_item") is False

    def test_write_check_precedes_read_rules(self, monkeypatch):
        """The write-list check must run BEFORE the list_/exact read checks.

        Regression guard: with no real write tool named list_*, deleting the
        write-list check leaves every other test green. This test fails if the
        ordering is ever inverted or the check removed.
        """
        import app.services.chat.celigo_tool_policy as policy

        monkeypatch.setitem(policy.CELIGO_WRITE_VERBS, "list_and_purge_flows", "delete")
        assert policy.is_read_only_celigo_tool("list_and_purge_flows") is False

        monkeypatch.setitem(policy.CELIGO_WRITE_VERBS, "get_schema", "update")
        assert policy.is_read_only_celigo_tool("get_schema") is False


class TestWriteVerbs:
    def test_every_write_tool_has_a_verb(self):
        for tool in ["upsert_flow", "patch_flow", "run_flow", "delete_resource"]:
            assert tool in CELIGO_WRITE_VERBS

    def test_verbs_stay_within_the_confirmation_literal(self):
        """write_confirmation_service.py:39 types mutation_type as
        Literal["create","update","delete","upsert"]. A verb outside that set
        raises a Pydantic ValidationError at runtime.
        """
        assert set(CELIGO_WRITE_VERBS.values()) <= {"create", "update", "delete", "upsert"}

    def test_no_write_tool_is_also_readable(self):
        """The two sets must never overlap, or the guards contradict each other."""
        for tool in CELIGO_WRITE_VERBS:
            assert is_read_only_celigo_tool(tool) is False
