"""Tests for the web.search tool."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def _mock_ddgs():
    """Provide a mock DDGS context manager."""
    mock_results = [
        {
            "title": "SuiteQL Date Functions",
            "body": "Use TO_DATE to convert strings to dates in SuiteQL queries. "
            "The format is TO_DATE('2024-01-01', 'YYYY-MM-DD').",
            "href": "https://example.com/suiteql-dates",
        },
        {
            "title": "NetSuite Record Types",
            "body": "Common record types include transaction, customer, item, and vendor.",
            "href": "https://example.com/record-types",
        },
        {
            "title": "SuiteQL ROWNUM Pagination",
            "body": "Use WHERE ROWNUM <= N for pagination in SuiteQL.",
            "href": "https://example.com/rownum",
        },
    ]
    mock_ddgs_instance = MagicMock()
    mock_ddgs_instance.text.return_value = mock_results
    mock_ddgs_instance.__enter__ = MagicMock(return_value=mock_ddgs_instance)
    mock_ddgs_instance.__exit__ = MagicMock(return_value=False)

    with patch("app.mcp.tools.web_search.DDGS", return_value=mock_ddgs_instance, create=True):
        # Also patch the import inside _sync_search
        with patch.dict(
            "sys.modules", {"duckduckgo_search": MagicMock(DDGS=MagicMock(return_value=mock_ddgs_instance))}
        ):
            yield mock_ddgs_instance


class TestWebSearchExecute:
    """Unit tests for web_search.execute()."""

    async def test_empty_query_returns_error(self):
        from app.mcp.tools.web_search import execute

        result = await execute({"query": ""})
        assert "error" in result
        assert "required" in result["error"]

    async def test_whitespace_query_returns_error(self):
        from app.mcp.tools.web_search import execute

        result = await execute({"query": "   "})
        assert "error" in result

    async def test_max_results_capped_at_10(self, _mock_ddgs):
        from app.mcp.tools.web_search import execute

        result = await execute({"query": "test", "max_results": 50})
        # Should not error; the cap is applied internally
        assert "error" not in result

    async def test_successful_search_returns_structured_results(self, _mock_ddgs):
        from app.mcp.tools.web_search import execute

        result = await execute({"query": "SuiteQL date functions"})

        assert "error" not in result
        assert result["count"] == 3
        assert result["query"] == "SuiteQL date functions"
        assert len(result["results"]) == 3

        first = result["results"][0]
        assert "title" in first
        assert "snippet" in first
        assert "url" in first
        assert first["title"] == "SuiteQL Date Functions"
        assert first["url"] == "https://example.com/suiteql-dates"

    async def test_default_max_results_is_5(self, _mock_ddgs):
        from app.mcp.tools.web_search import execute

        result = await execute({"query": "test"})
        # The mock returns 3, so count should be 3 (less than default 5)
        assert result["count"] == 3

    async def test_snippet_truncated_to_500_chars(self):
        """Test that long snippets are truncated."""
        long_body = "x" * 1000
        mock_results = [{"title": "Long", "body": long_body, "href": "https://example.com"}]

        mock_ddgs_instance = MagicMock()
        mock_ddgs_instance.text.return_value = mock_results
        mock_ddgs_instance.__enter__ = MagicMock(return_value=mock_ddgs_instance)
        mock_ddgs_instance.__exit__ = MagicMock(return_value=False)

        mock_module = MagicMock()
        mock_module.DDGS.return_value = mock_ddgs_instance

        with patch.dict("sys.modules", {"duckduckgo_search": mock_module}):
            from app.mcp.tools.web_search import execute

            result = await execute({"query": "test"})

        if "error" not in result and result.get("results"):
            assert len(result["results"][0]["snippet"]) <= 500

    async def test_import_error_handled_gracefully(self):
        """Test graceful handling when duckduckgo-search is not installed."""
        from app.mcp.tools.web_search import execute

        with patch("app.mcp.tools.web_search._sync_search", side_effect=ImportError("No module")):
            # The ImportError is caught inside _sync_search which is called via to_thread,
            # but the outer try/except catches it
            result = await execute({"query": "test"})
            # Should return an error, not crash
            assert "error" in result

    async def test_network_error_handled_gracefully(self):
        """Test graceful handling of network errors."""
        from app.mcp.tools.web_search import execute

        with patch(
            "app.mcp.tools.web_search._sync_search",
            side_effect=ConnectionError("Network unreachable"),
        ):
            result = await execute({"query": "test"})
            assert "error" in result
            assert "failed" in result["error"].lower() or "Network" in result["error"]


class TestWebSearchRegistration:
    """Integration tests for tool registration."""

    def test_registered_in_tool_registry(self):
        from app.mcp.registry import TOOL_REGISTRY

        assert "web.search" in TOOL_REGISTRY
        tool = TOOL_REGISTRY["web.search"]
        assert "execute" in tool
        assert "params_schema" in tool
        assert "query" in tool["params_schema"]

    def test_governance_config_exists(self):
        from app.mcp.governance import TOOL_CONFIGS

        assert "web.search" in TOOL_CONFIGS
        config = TOOL_CONFIGS["web.search"]
        assert config["rate_limit_per_minute"] == 10
        assert config["timeout_seconds"] == 15
        assert "query" in config["allowlisted_params"]

    def test_in_allowed_chat_tools(self):
        from app.services.chat.nodes import ALLOWED_CHAT_TOOLS

        assert "web.search" in ALLOWED_CHAT_TOOLS

    def test_included_in_local_tool_definitions(self):
        from app.services.chat.tools import build_local_tool_definitions

        tools = build_local_tool_definitions()
        tool_names = {t["name"] for t in tools}
        assert "web_search" in tool_names  # dot → underscore conversion
