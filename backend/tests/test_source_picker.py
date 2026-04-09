"""Tests for the source picker confidence scorer."""

from __future__ import annotations

import pytest

from app.services.chat.source_picker import (
    AMBIGUITY_THRESHOLD,
    SourceScore,
    build_picker_payload,
    has_data_intent,
    score_source,
    should_prompt_user,
)


class TestScoreSourceFinancial:
    @pytest.mark.parametrize(
        "query",
        [
            "income statement for Q1",
            "income statements by subsidiary",
            "show me the balance sheet",
            "P&L for last month",
            "profit and loss",
            "trial balance",
            "general ledger by account",
            "gl summary last month",
            "ebitda trend",
            "consolidated financials",
            "chart of accounts",
        ],
    )
    def test_financial_keywords_route_to_netsuite(self, query):
        source, confidence, reason = score_source(query)
        assert source == "netsuite"
        assert confidence >= 0.95
        assert "financial" in reason.lower() or "ledger" in reason.lower() or "statement" in reason.lower()


class TestScoreSourceNetSuiteEntities:
    @pytest.mark.parametrize(
        "query",
        [
            "outstanding balance for Acme",
            "AR aging report",
            "open invoices for customer X",
            "vendor bills due this week",
            "journal entries last month",
            "credit memos issued in March",
        ],
    )
    def test_netsuite_entities_route_to_netsuite(self, query):
        source, confidence, _ = score_source(query)
        assert source == "netsuite"
        assert confidence >= 0.9


class TestScoreSourceNetSuiteOperational:
    @pytest.mark.parametrize(
        "query",
        [
            "inventory adjustment last week",
            "purchase order status",
            "item receipt for PO 1234",
            "subsidiary breakdown",
            "department spend this quarter",
        ],
    )
    def test_netsuite_operational_route_to_netsuite(self, query):
        source, confidence, _ = score_source(query)
        assert source == "netsuite"
        assert confidence >= 0.9


class TestScoreSourceMarketing:
    @pytest.mark.parametrize(
        "query",
        [
            "ad spend by campaign",
            "attribution breakdown this month",
            "conversion rate by channel",
            "cohort analysis for Q1 acquisitions",
            "UTM source distribution",
            "CAC by acquisition channel",
            "ROAS this quarter",
        ],
    )
    def test_marketing_keywords_route_to_bigquery(self, query):
        source, confidence, _ = score_source(query)
        assert source == "bigquery"
        assert confidence >= 0.9


class TestScoreSourceAmbiguous:
    @pytest.mark.parametrize(
        "query",
        [
            "how many orders this week",
            "top customers",
            "show me recent transactions",
            "sales last month",
            "orders by product",
            "customer list",
            "revenue this quarter",
        ],
    )
    def test_ambiguous_queries_below_threshold(self, query):
        source, confidence, _ = score_source(query)
        # Default recommendation should be NetSuite (source of truth)
        assert source == "netsuite"
        assert confidence < AMBIGUITY_THRESHOLD
        assert should_prompt_user((source, confidence, ""))


class TestScoreSourceExplicitMention:
    def test_explicit_bigquery_mention(self):
        source, confidence, _ = score_source("run this in BigQuery: how many orders")
        assert source == "bigquery"
        assert confidence >= 0.95

    def test_explicit_netsuite_mention(self):
        source, confidence, _ = score_source("use netsuite to show me orders")
        assert source == "netsuite"
        assert confidence >= 0.95


class TestShouldPromptUser:
    def test_high_confidence_no_prompt(self):
        score: SourceScore = ("netsuite", 0.99, "financial")
        assert should_prompt_user(score) is False

    def test_at_threshold_no_prompt(self):
        score: SourceScore = ("netsuite", AMBIGUITY_THRESHOLD, "x")
        assert should_prompt_user(score) is False

    def test_below_threshold_prompt(self):
        score: SourceScore = ("netsuite", AMBIGUITY_THRESHOLD - 0.01, "x")
        assert should_prompt_user(score) is True

    def test_low_confidence_prompts(self):
        score: SourceScore = ("netsuite", 0.5, "unclear")
        assert should_prompt_user(score) is True


class TestHasDataIntent:
    @pytest.mark.parametrize(
        "query",
        [
            "how many orders this week",
            "top customers",
            "show me recent transactions",
            "sales last month",
            "revenue this quarter",
            "total invoices",
            "count of refunds",
            "trend of payouts YTD",
            "Q1 revenue report",
            "ad spend last week",
            "income statement",
            "open invoices for customer X",
            "run this in bigquery",
        ],
    )
    def test_data_queries_have_intent(self, query):
        assert has_data_intent(query) is True

    @pytest.mark.parametrize(
        "query",
        [
            "hello",
            "hi there",
            "thanks",
            "how are you",
            "what can you do",
            "help me",
            "show me workspace files",
            "upload a file",
            "",
            "   ",
        ],
    )
    def test_non_data_queries_lack_intent(self, query):
        assert has_data_intent(query) is False


class TestBuildPickerPayload:
    def test_payload_contains_both_options(self):
        score: SourceScore = ("netsuite", 0.55, "operational data")
        payload = build_picker_payload(score, user_question="how many orders this week")
        assert payload["type"] == "source_picker"
        assert payload["recommended"] == "netsuite"
        assert payload["user_question"] == "how many orders this week"
        assert len(payload["options"]) == 2
        sources = [o["source"] for o in payload["options"]]
        assert "netsuite" in sources
        assert "bigquery" in sources

    def test_recommended_option_is_flagged(self):
        score: SourceScore = ("netsuite", 0.55, "operational data")
        payload = build_picker_payload(score, user_question="test")
        ns_option = next(o for o in payload["options"] if o["source"] == "netsuite")
        bq_option = next(o for o in payload["options"] if o["source"] == "bigquery")
        assert ns_option["recommended"] is True
        assert bq_option["recommended"] is False

    def test_label_and_description_present(self):
        score: SourceScore = ("netsuite", 0.55, "operational")
        payload = build_picker_payload(score, user_question="test")
        for opt in payload["options"]:
            assert isinstance(opt["label"], str) and opt["label"]
            assert isinstance(opt["description"], str) and opt["description"]
