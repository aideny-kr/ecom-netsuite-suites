"""Tests for SuiteQL agent prompt rules and metadata reference builder."""

from app.services.chat.agents.suiteql_agent import _SYSTEM_PROMPT, SuiteQLAgent


class TestSuiteQLPromptRules:
    """Verify critical prompt rules are present."""

    def test_tranid_convention_preserved(self):
        """Agent should search tranid with exact value including prefix."""
        assert "tranid" in _SYSTEM_PROMPT
        assert "RMA" in _SYSTEM_PROMPT
        assert "RtnAuth" in _SYSTEM_PROMPT

    def test_local_tool_preferred(self):
        """Agent should prefer netsuite_suiteql (local REST) as default."""
        assert "USE THIS AS DEFAULT" in _SYSTEM_PROMPT
        assert "netsuite_suiteql" in _SYSTEM_PROMPT

    def test_mcp_is_fallback(self):
        """MCP tool should be fallback only."""
        assert "fallback" in _SYSTEM_PROMPT.lower()

    def test_custom_list_field_rules(self):
        """Agent should know about SELECT fields and list value IDs."""
        assert "CUSTOM LIST FIELDS" in _SYSTEM_PROMPT
        assert "SELECT → customlist" in _SYSTEM_PROMPT or "SELECT →" in _SYSTEM_PROMPT

    def test_field_to_list_linkage_mentioned(self):
        assert "field-to-list linkage" in _SYSTEM_PROMPT.lower() or "SELECT → customlist" in _SYSTEM_PROMPT


class TestMetadataReference:
    """Test _build_metadata_reference field-to-list linkage."""

    def test_select_field_linkage(self):
        agent = SuiteQLAgent.__new__(SuiteQLAgent)

        class FakeMD:
            transaction_body_fields = [
                {
                    "scriptid": "custbody_status",
                    "name": "Status",
                    "fieldtype": "SELECT",
                    "fieldvaluetype": "customlist_order_status",
                },
            ]
            transaction_column_fields = None
            entity_custom_fields = None
            item_custom_fields = None
            custom_record_types = None
            custom_record_fields = None
            custom_lists = None
            subsidiaries = None
            departments = None
            classifications = None
            locations = None
            scripts = None
            script_deployments = None
            workflows = None
            custom_list_values = {
                "customlist_order_status": [
                    {"id": 1, "name": "Pending"},
                    {"id": 2, "name": "Failed"},
                ]
            }
            saved_searches = None

        agent._metadata = FakeMD()
        result = agent._build_metadata_reference()
        assert "SELECT → customlist_order_status" in result
        assert "'Pending': ID 1" in result
        assert "'Failed': ID 2" in result
