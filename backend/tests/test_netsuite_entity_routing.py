"""Tests for R6: NetSuite entity routing — force unified agent for NS-native queries."""

import pytest

from app.services.chat.orchestrator import _detect_netsuite_entity


class TestNetSuiteEntityDetection:
    """Detect queries about NetSuite-native entities that don't exist in BigQuery."""

    @pytest.mark.parametrize(
        "query",
        [
            "top 5 customers by outstanding balance",
            "show me customer balances over 10000",
            "open invoices aging over 90 days",
            "AR aging report by customer",
            "accounts receivable summary",
            "list open sales orders",
            "show me vendor bills pending approval",
            "journal entries posted this month",
            "credit memo for customer ABC",
            "what's the GL balance for account 1200",
            "show me open purchase orders",
        ],
    )
    def test_detects_netsuite_entity(self, query: str):
        assert _detect_netsuite_entity(query) is True

    @pytest.mark.parametrize(
        "query",
        [
            "total revenue by month from BigQuery",
            "show me order trends over time",
            "what's our top selling product",
            "average order value last quarter",
            "how many orders per day",
            "revenue by region",
        ],
    )
    def test_ignores_generic_analytics(self, query: str):
        assert _detect_netsuite_entity(query) is False
