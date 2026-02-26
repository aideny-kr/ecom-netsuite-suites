"""Tests for JIT context injection — lean synthesis, tiered metadata, XML structure."""

import uuid
from unittest.mock import AsyncMock, MagicMock

from app.services.chat.agents.suiteql_agent import _SCRIPT_KEYWORDS, SuiteQLAgent
from app.services.chat.coordinator import COORDINATOR_SYNTHESIS_PROMPT, MultiAgentCoordinator


def _make_coordinator(**kwargs):
    """Create a minimal coordinator for testing."""
    return MultiAgentCoordinator(
        db=AsyncMock(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        correlation_id="test",
        main_adapter=kwargs.get("main_adapter", AsyncMock()),
        main_model="test-main",
        specialist_adapter=AsyncMock(),
        specialist_model="test-spec",
    )


def _make_metadata(**overrides):
    """Build a mock NetSuiteMetadata object."""
    md = MagicMock()
    md.transaction_body_fields = overrides.get("transaction_body_fields", [
        {"scriptid": "custbody_channel", "name": "Sales Channel", "fieldtype": "SELECT"},
    ])
    md.transaction_column_fields = overrides.get("transaction_column_fields", [])
    md.entity_custom_fields = overrides.get("entity_custom_fields", [])
    md.item_custom_fields = overrides.get("item_custom_fields", [])
    md.custom_record_types = overrides.get("custom_record_types", [])
    md.custom_record_fields = overrides.get("custom_record_fields", [])
    md.subsidiaries = overrides.get("subsidiaries", [])
    md.departments = overrides.get("departments", [])
    md.classifications = overrides.get("classifications", [])
    md.locations = overrides.get("locations", [])
    md.scripts = overrides.get("scripts", [
        {"scriptid": "customscript_foo", "scripttype": "USEREVENT", "name": "Foo Script"},
    ])
    md.script_deployments = overrides.get("script_deployments", [
        {
            "scriptid": "customdeploy_foo", "title": "Foo Deploy",
            "recordtype": "salesorder", "status": "Released", "script": "customscript_foo",
        },
    ])
    md.workflows = overrides.get("workflows", [
        {"scriptid": "customworkflow_bar", "recordtype": "salesorder", "status": "Released", "name": "Bar WF"},
    ])
    md.custom_list_values = overrides.get("custom_list_values", {})
    md.saved_searches = overrides.get("saved_searches", [])
    return md


# ── Lean synthesis prompt tests ──


class TestLeanSynthesisPrompt:
    def test_synthesis_prompt_does_not_contain_suiteql_rules(self):
        """Lean synthesis prompt should NOT include SuiteQL syntax or tool inventory."""
        coord = _make_coordinator()
        prompt = coord._build_synthesis_system_prompt()
        assert "netsuite_suiteql" not in prompt
        assert "ROWNUM" not in prompt
        assert "netsuite_get_metadata" not in prompt

    def test_synthesis_prompt_contains_core_directives(self):
        """Lean synthesis prompt should contain persona and constraints."""
        coord = _make_coordinator()
        prompt = coord._build_synthesis_system_prompt()
        assert "<system_directives>" in prompt
        assert "<persona>" in prompt
        assert "Never fabricate" in prompt

    def test_soul_tone_in_synthesis(self):
        """Soul tone should be injected into synthesis prompt."""
        coord = _make_coordinator()
        coord.soul_tone = "Be friendly and use emojis."
        prompt = coord._build_synthesis_system_prompt()
        assert "Be friendly and use emojis." in prompt
        assert "<tenant_context>" in prompt

    def test_soul_tone_excluded_when_empty(self):
        """No tenant_context section when soul_tone is empty."""
        coord = _make_coordinator()
        coord.soul_tone = ""
        prompt = coord._build_synthesis_system_prompt()
        assert "<tenant_context>" not in prompt

    def test_synthesis_prompt_includes_synthesis_rules(self):
        """Lean synthesis prompt should include the COORDINATOR_SYNTHESIS_PROMPT."""
        coord = _make_coordinator()
        prompt = coord._build_synthesis_system_prompt()
        assert "<synthesis_rules>" in prompt


# ── Tiered metadata tests ──


class TestTieredMetadata:
    def test_metadata_tier1_always_present(self):
        """Custom fields should always be in the metadata reference."""
        agent = SuiteQLAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
            metadata=_make_metadata(),
        )
        ref = agent._build_metadata_reference(task="show me today's orders")
        assert "custbody_channel" in ref

    def test_metadata_tier2_excluded_for_data_query(self):
        """Scripts/deployments/workflows should NOT appear for normal data queries."""
        agent = SuiteQLAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
            metadata=_make_metadata(),
        )
        ref = agent._build_metadata_reference(task="show me today's orders")
        assert "customscript_foo" not in ref
        assert "customdeploy_foo" not in ref
        assert "customworkflow_bar" not in ref
        # But should mention availability
        assert "scripts" in ref.lower()

    def test_metadata_tier2_included_for_script_query(self):
        """Scripts should appear when task mentions scripts."""
        agent = SuiteQLAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
            metadata=_make_metadata(),
        )
        ref = agent._build_metadata_reference(task="list all user event scripts")
        assert "customscript_foo" in ref
        assert "customdeploy_foo" in ref

    def test_metadata_tier2_workflow_keyword(self):
        """Workflows should appear when task mentions workflows."""
        agent = SuiteQLAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
            metadata=_make_metadata(),
        )
        ref = agent._build_metadata_reference(task="show me all workflows on sales orders")
        assert "customworkflow_bar" in ref

    def test_script_keywords_regex(self):
        """Verify _SCRIPT_KEYWORDS matches expected patterns."""
        assert _SCRIPT_KEYWORDS.search("list all scripts")
        assert _SCRIPT_KEYWORDS.search("show workflow triggers")
        assert _SCRIPT_KEYWORDS.search("user event on sales order")
        assert _SCRIPT_KEYWORDS.search("what restlet handles this?")
        assert _SCRIPT_KEYWORDS.search("map reduce status")
        assert not _SCRIPT_KEYWORDS.search("show me today's orders")
        assert not _SCRIPT_KEYWORDS.search("revenue by month")


# ── Header vs line aggregation prompt tests ──


class TestHeaderLineAggregationRule:
    def test_suiteql_prompt_warns_against_header_sum_with_line_join(self):
        """SuiteQL system prompt should warn about SUM(t.foreigntotal) with transactionline joins."""
        from app.services.chat.agents.suiteql_agent import _SYSTEM_PROMPT

        assert "NEVER use `SUM(t.foreigntotal)`" in _SYSTEM_PROMPT
        assert "SUM(tl.foreignamount)" in _SYSTEM_PROMPT
        assert "HEADER vs LINE AGGREGATION" in _SYSTEM_PROMPT


# ── XML structure tests ──


class TestXMLStructure:
    def test_synthesis_prompt_has_xml_tags(self):
        """COORDINATOR_SYNTHESIS_PROMPT should use XML structure."""
        assert "<synthesis_rules>" in COORDINATOR_SYNTHESIS_PROMPT
        assert "</synthesis_rules>" in COORDINATOR_SYNTHESIS_PROMPT
        assert "<format>" in COORDINATOR_SYNTHESIS_PROMPT
        assert "<constraints>" in COORDINATOR_SYNTHESIS_PROMPT

    def test_agentic_prompt_has_xml_tags(self):
        """AGENTIC_SYSTEM_PROMPT should use XML structure."""
        from app.services.chat.prompts import AGENTIC_SYSTEM_PROMPT
        assert "<system_directives>" in AGENTIC_SYSTEM_PROMPT
        assert "<persona>" in AGENTIC_SYSTEM_PROMPT
        assert "<tool_inventory>" in AGENTIC_SYSTEM_PROMPT
        assert "<domain_rules>" in AGENTIC_SYSTEM_PROMPT
        assert "<suiteql_syntax>" in AGENTIC_SYSTEM_PROMPT


# ── Tool error truncation tests ──


class TestTruncateToolResult:
    def test_truncates_long_error_message(self):
        """Long NetSuite error messages should be truncated to ~1000 chars."""
        import json

        from app.services.chat.agents.base_agent import _truncate_tool_result

        big_error = json.dumps({"error": True, "message": "x" * 5000})
        result = _truncate_tool_result(big_error)
        parsed = json.loads(result)
        assert len(parsed["message"]) < 1100
        assert "truncated" in parsed["message"]

    def test_preserves_small_success_results(self):
        """Small successful tool results should not be truncated."""
        import json

        from app.services.chat.agents.base_agent import _truncate_tool_result

        small_success = json.dumps({"items": [{"id": i} for i in range(10)]})
        result = _truncate_tool_result(small_success)
        assert result == small_success

    def test_truncates_large_rows(self):
        """Large row-based results should be capped at 50 rows."""
        import json

        from app.services.chat.agents.base_agent import _MAX_RESULT_ROWS, _truncate_tool_result

        big_result = json.dumps({
            "columns": ["id", "name"],
            "rows": [[i, f"item_{i}"] for i in range(257)],
            "row_count": 257,
        })
        result = _truncate_tool_result(big_result)
        parsed = json.loads(result)
        assert len(parsed["rows"]) == _MAX_RESULT_ROWS
        assert parsed["rows_truncated"] is True
        assert parsed["row_count"] == 257
        assert "GROUP BY" in parsed["_warning"]

    def test_truncates_large_items(self):
        """Large items arrays should be capped at 50."""
        import json

        from app.services.chat.agents.base_agent import _MAX_RESULT_ROWS, _truncate_tool_result

        big_items = json.dumps({"items": [{"id": i} for i in range(200)]})
        result = _truncate_tool_result(big_items)
        parsed = json.loads(result)
        assert len(parsed["items"]) == _MAX_RESULT_ROWS
        assert parsed["items_truncated"] is True
        assert "GROUP BY" in parsed["_warning"]

    def test_preserves_short_errors(self):
        """Short error messages should pass through unchanged."""
        import json

        from app.services.chat.agents.base_agent import _truncate_tool_result

        short_error = json.dumps({"error": True, "message": "Unknown identifier"})
        result = _truncate_tool_result(short_error)
        assert result == short_error

    def test_handles_non_json(self):
        """Non-JSON strings should pass through."""
        from app.services.chat.agents.base_agent import _truncate_tool_result

        assert _truncate_tool_result("plain text") == "plain text"

    def test_backward_compat_alias(self):
        """_truncate_error_payload should still work as alias."""
        from app.services.chat.agents.base_agent import _truncate_error_payload, _truncate_tool_result

        assert _truncate_error_payload is _truncate_tool_result
