"""Tests for R1: Explicit web search override."""

import pytest

from app.services.chat.orchestrator import _detect_web_search_intent


class TestWebSearchDetection:
    """Regex detection for explicit web search requests."""

    @pytest.mark.parametrize(
        "query",
        [
            "search the web for NetSuite API changes",
            "you should now search web",
            "look this up online",
            "use web search to find pricing",
            "google this for me",
            "search online for SuiteQL date functions",
            "can you web search for this error",
            "look up online what this means",
        ],
    )
    def test_detects_explicit_web_search(self, query: str):
        assert _detect_web_search_intent(query) is True

    @pytest.mark.parametrize(
        "query",
        [
            "search for all open sales orders",
            "find customers with balance over 10000",
            "look up order SO-12345",
            "what's the total revenue this month",
            "search the saved searches",
            "web application performance",
        ],
    )
    def test_ignores_data_queries(self, query: str):
        assert _detect_web_search_intent(query) is False
