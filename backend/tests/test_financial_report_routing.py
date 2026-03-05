"""Tests for FINANCIAL_REPORT intent classification and routing."""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.services.chat.coordinator import (
    IntentType,
    MultiAgentCoordinator,
    ROUTE_REGISTRY,
    classify_intent,
)


# ── classify_intent tests ──


class TestFinancialReportClassification:
    """Verify financial queries are classified as FINANCIAL_REPORT."""

    @pytest.mark.parametrize(
        "query",
        [
            "Show me the income statement for Q4 2025",
            "What's our P&L this month?",
            "profit and loss for January",
            "Generate the balance sheet as of December 31",
            "Show me the trial balance for this period",
            "What's the net income for FY2025?",
            "gross margin by account",
            "What are our operating expenses?",
            "consolidated revenue for the group",
            "financial statement for Q3",
            "financial report for last quarter",
            "Show me the general ledger summary",
            "GL impact for this month",
            "Show me the chart of accounts",
            "EBITDA for this fiscal year",
            "revenue by account for January",
            "expenses by GL category",
            "cash flow statement for Q1 2026",
            "financial performance this year",
            "consolidated financials for 2025",
            "gross profit for last month",
            "cogs by account type",
            "cost of goods sold by period",
            "fiscal year report summary",
        ],
    )
    def test_financial_queries_classified_correctly(self, query):
        assert classify_intent(query) == IntentType.FINANCIAL_REPORT, (
            f"Expected FINANCIAL_REPORT for: {query!r}"
        )

    @pytest.mark.parametrize(
        "query,expected",
        [
            # These should stay as DATA_QUERY (operational, not financial statement)
            ("show me today's sales orders", IntentType.DATA_QUERY),
            ("find invoice INV12345", IntentType.DATA_QUERY),
            ("how many orders this week", IntentType.DATA_QUERY),
            ("revenue by platform this month", IntentType.DATA_QUERY),
            ("accounts receivable aging", IntentType.DATA_QUERY),
            ("balance due on order SO12345", IntentType.DATA_QUERY),
            # Analysis intent
            ("compare sales Q3 vs Q4", IntentType.ANALYSIS),
            ("month over month trend", IntentType.ANALYSIS),
            # Code understanding intent (writing scripts is CODE_UNDERSTANDING)
            ("how do I write a SuiteScript", IntentType.CODE_UNDERSTANDING),
            # Documentation intent
            ("what is SuiteQL syntax for dates", IntentType.DOCUMENTATION),
        ],
    )
    def test_non_financial_queries_not_misrouted(self, query, expected):
        result = classify_intent(query)
        assert result == expected, (
            f"Expected {expected.value} for: {query!r}, got {result.value}"
        )


# ── Route registry tests ──


class TestFinancialReportRouteConfig:
    """Verify FINANCIAL_REPORT has correct routing configuration."""

    def test_route_exists(self):
        assert IntentType.FINANCIAL_REPORT in ROUTE_REGISTRY

    def test_route_agents(self):
        route = ROUTE_REGISTRY[IntentType.FINANCIAL_REPORT]
        assert route.agents == ["suiteql", "analysis"]

    def test_route_is_sequential(self):
        route = ROUTE_REGISTRY[IntentType.FINANCIAL_REPORT]
        assert route.parallel is False


# ── Plan building tests ──


class TestFinancialReportPlanBuilding:
    """Verify _build_plan_from_intent adds financial framing."""

    def _make_coordinator(self):
        return MultiAgentCoordinator(
            db=AsyncMock(),
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
            main_adapter=AsyncMock(),
            main_model="test-main",
            specialist_adapter=AsyncMock(),
            specialist_model="test-spec",
        )

    def test_financial_plan_has_two_steps(self):
        coord = self._make_coordinator()
        plan = coord._build_plan_from_intent(
            IntentType.FINANCIAL_REPORT, "Show me the P&L for January"
        )
        assert len(plan.steps) == 2
        assert plan.steps[0].agent == "suiteql"
        assert plan.steps[1].agent == "analysis"

    def test_financial_plan_augments_suiteql_task(self):
        coord = self._make_coordinator()
        plan = coord._build_plan_from_intent(
            IntentType.FINANCIAL_REPORT, "Show me the P&L for January"
        )
        task = plan.steps[0].task
        assert "FINANCIAL REPORT MODE" in task
        assert "TransactionAccountingLine" in task
        assert "inception-to-date" in task

    def test_financial_plan_includes_accountingbook_filter(self):
        coord = self._make_coordinator()
        plan = coord._build_plan_from_intent(
            IntentType.FINANCIAL_REPORT, "Show me the P&L for January"
        )
        task = plan.steps[0].task
        assert "accountingbook" in task.lower()

    def test_financial_plan_includes_posting_filter(self):
        coord = self._make_coordinator()
        plan = coord._build_plan_from_intent(
            IntentType.FINANCIAL_REPORT, "Show me the P&L for January"
        )
        task = plan.steps[0].task
        assert "posting" in task.lower()

    def test_financial_plan_includes_postingperiod(self):
        coord = self._make_coordinator()
        plan = coord._build_plan_from_intent(
            IntentType.FINANCIAL_REPORT, "Show me the P&L for January"
        )
        task = plan.steps[0].task
        assert "postingperiod" in task.lower()

    def test_financial_plan_mentions_consolidate(self):
        coord = self._make_coordinator()
        plan = coord._build_plan_from_intent(
            IntentType.FINANCIAL_REPORT, "Show me the P&L for January"
        )
        task = plan.steps[0].task
        assert "CONSOLIDATE" in task

    def test_financial_plan_preserves_user_message(self):
        coord = self._make_coordinator()
        user_msg = "Show me the balance sheet as of Dec 31"
        plan = coord._build_plan_from_intent(IntentType.FINANCIAL_REPORT, user_msg)
        assert user_msg in plan.steps[0].task

    def test_data_query_plan_does_not_augment(self):
        coord = self._make_coordinator()
        user_msg = "show me today's orders"
        plan = coord._build_plan_from_intent(IntentType.DATA_QUERY, user_msg)
        assert plan.steps[0].task == user_msg
        assert "FINANCIAL REPORT MODE" not in plan.steps[0].task

    def test_financial_plan_intent_is_set(self):
        coord = self._make_coordinator()
        plan = coord._build_plan_from_intent(
            IntentType.FINANCIAL_REPORT, "income statement"
        )
        assert plan.intent == IntentType.FINANCIAL_REPORT
        assert plan.used_heuristic is True


# ── Allowed tables config test ──


class TestAllowedTablesConfig:
    """Verify financial reporting tables are in the allowed list."""

    def test_accountingperiod_allowed(self):
        from app.core.config import settings

        tables = settings.NETSUITE_SUITEQL_ALLOWED_TABLES.split(",")
        assert "accountingperiod" in tables

    def test_accountingbook_allowed(self):
        from app.core.config import settings

        tables = settings.NETSUITE_SUITEQL_ALLOWED_TABLES.split(",")
        assert "accountingbook" in tables

    def test_transactionaccountingline_allowed(self):
        from app.core.config import settings

        tables = settings.NETSUITE_SUITEQL_ALLOWED_TABLES.split(",")
        assert "transactionaccountingline" in tables

    def test_account_allowed(self):
        from app.core.config import settings

        tables = settings.NETSUITE_SUITEQL_ALLOWED_TABLES.split(",")
        assert "account" in tables
