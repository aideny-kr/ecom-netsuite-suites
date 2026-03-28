"""Tests for R5: Reduce re-query bias — detect data references."""

import pytest

from app.services.chat.orchestrator import _detect_data_reference


class TestDataReferenceDetection:
    """Detect when user references previously-returned data."""

    @pytest.mark.parametrize(
        "query",
        [
            "the list we pulled earlier",
            "use the same data",
            "from the previous result",
            "the customers we just looked up",
            "take that result and enrich it",
            "using the data above",
            "with the same list",
            "the orders from before",
            "use those results",
            "based on what we just pulled",
        ],
    )
    def test_detects_data_reference(self, query: str):
        assert _detect_data_reference(query) is True

    @pytest.mark.parametrize(
        "query",
        [
            "show me all open sales orders",
            "what's the total revenue",
            "pull a list of customers",
            "how many orders last month",
        ],
    )
    def test_ignores_new_data_requests(self, query: str):
        assert _detect_data_reference(query) is False
