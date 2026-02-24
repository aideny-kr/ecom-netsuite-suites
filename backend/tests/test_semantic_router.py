"""Tests for the semantic routing engine in coordinator.py.

Tests the heuristic classifier (classify_intent) and route registry
to ensure user messages are routed to the correct specialist agent
without unnecessary LLM calls.
"""

import pytest

from app.services.chat.coordinator import (
    ROUTE_REGISTRY,
    IntentType,
    classify_intent,
)

# ── DATA_QUERY intent ──────────────────────────────────────────────────────


class TestDataQueryClassification:
    """Messages that should route to the SuiteQL agent."""

    @pytest.mark.parametrize(
        "message",
        [
            "show me the latest 10 sales orders",
            "get order #865732",
            "find customer Acme Corp",
            "what's the total revenue this month",
            "pull the last 5 invoices",
            "look up SO12345",
            "tell me about #865732 shopify order",
            "how many orders were placed today",
            "latest order from netsuite",
            "fetch all open purchase orders",
            "show me pending invoices",
            "sales total for last quarter",
            "revenue by subsidiary this year",
            "inventory levels for SKU-1234",
            "accounts receivable balance",
            "run a suiteql query for customers",
            "#12345",
            "INV90032",
            "SO8228253",
            "find the shopify order number 898657326",
        ],
    )
    def test_data_query_classified(self, message: str) -> None:
        assert classify_intent(message) == IntentType.DATA_QUERY


# ── DOCUMENTATION intent ───────────────────────────────────────────────────


class TestDocumentationClassification:
    """Messages that should route to the RAG agent."""

    @pytest.mark.parametrize(
        "message",
        [
            "how do I use the N/record module",
            "what is SuiteQL syntax for date filtering",
            "explain the difference between saved search and SuiteQL",
            "SuiteScript API reference for N/file",
            "what does error code INVALID_FLD_VALUE mean",
            "netsuite documentation for custom records",
            "how can I use BUILTIN.DF in SuiteQL",
            "what tables are available in SuiteQL",
            "governance limit for scheduled scripts",
            "how can I query dates in SuiteQL",
        ],
    )
    def test_documentation_classified(self, message: str) -> None:
        assert classify_intent(message) == IntentType.DOCUMENTATION


# ── WORKSPACE_DEV intent ───────────────────────────────────────────────────


class TestWorkspaceDevClassification:
    """Messages that should route to the workspace agent."""

    @pytest.mark.parametrize(
        "message",
        [
            "write a SuiteScript user event script for order validation",
            "review the changeset for the RESTlet update",
            "create a jest test for the file cabinet module",
            "refactor the scheduled script to use map/reduce",
            "list the files in my workspace",
            "read the restlet file",
            "search the workspace for custbody references",
            "propose a patch to fix the error handler",
            "write a unit test for the sync logic",
            "search the scripts for entity references",
            "create a suitelet for the custom report",
            "write a client script for field validation",
        ],
    )
    def test_workspace_classified(self, message: str) -> None:
        assert classify_intent(message) == IntentType.WORKSPACE_DEV


# ── ANALYSIS intent ────────────────────────────────────────────────────────


class TestAnalysisClassification:
    """Messages that should route to suiteql → analysis pipeline."""

    @pytest.mark.parametrize(
        "message",
        [
            "compare sales between Q1 and Q2",
            "month-over-month revenue trend",
            "top 10 customers by order volume",
            "analyze the sales data by category",
            "year-over-year growth rate",
            "breakdown of revenue by subsidiary",
        ],
    )
    def test_analysis_classified(self, message: str) -> None:
        assert classify_intent(message) == IntentType.ANALYSIS


# ── AMBIGUOUS intent ───────────────────────────────────────────────────────


class TestAmbiguousClassification:
    """Messages that don't match any heuristic and need LLM fallback."""

    @pytest.mark.parametrize(
        "message",
        [
            "hello",
            "thanks",
            "can you help me with something",
            "what should I do next",
            "tell me more",
        ],
    )
    def test_ambiguous_classified(self, message: str) -> None:
        assert classify_intent(message) == IntentType.AMBIGUOUS


# ── Route registry ─────────────────────────────────────────────────────────


class TestRouteRegistry:
    """Verify the route registry maps intents to the right agents."""

    def test_documentation_route(self) -> None:
        route = ROUTE_REGISTRY[IntentType.DOCUMENTATION]
        assert route.agents == ["rag"]
        assert route.parallel is False

    def test_data_query_route(self) -> None:
        route = ROUTE_REGISTRY[IntentType.DATA_QUERY]
        assert route.agents == ["suiteql"]
        assert route.parallel is False

    def test_workspace_route(self) -> None:
        route = ROUTE_REGISTRY[IntentType.WORKSPACE_DEV]
        assert route.agents == ["workspace"]
        assert route.parallel is False

    def test_analysis_route(self) -> None:
        route = ROUTE_REGISTRY[IntentType.ANALYSIS]
        assert route.agents == ["suiteql", "analysis"]
        assert route.parallel is False

    def test_all_intents_have_routes(self) -> None:
        """Every non-AMBIGUOUS intent should have a route config."""
        for intent in IntentType:
            if intent != IntentType.AMBIGUOUS:
                assert intent in ROUTE_REGISTRY, f"Missing route for {intent}"


# ── Bare number shortcut ──────────────────────────────────────────────────


class TestBareNumberShortcut:
    """Bare number inputs (like order IDs) should fast-path to DATA_QUERY."""

    @pytest.mark.parametrize(
        "message",
        [
            "#12345",
            "12345678",
            "#898657326",
        ],
    )
    def test_bare_number(self, message: str) -> None:
        assert classify_intent(message) == IntentType.DATA_QUERY
