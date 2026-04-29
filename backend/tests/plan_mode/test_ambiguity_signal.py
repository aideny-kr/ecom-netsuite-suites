"""Test the financial-ambiguity detector regex + connector-gate logic."""

import pytest

from app.services.chat.plan_mode.ambiguity_signal import is_financial_ambiguous


@pytest.mark.parametrize(
    "query",
    [
        "What's our revenue this quarter?",
        "How's our gross margin tracking?",
        "Show me MRR for last 3 months",
        "Top line for Q2",
        "GMV breakdown by month",
        "EBITDA YTD",
        "Net income last fiscal year",
        "What was bookings vs ARR?",
        "Operating income trend",
        "Recognized revenue in May",
        "Earnings this quarter",
        "Cogs as % of net sales",
    ],
)
def test_detects_financial_ambiguity(query):
    assert is_financial_ambiguous(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "How many sales orders today?",
        "Top 10 customers by item count",
        "Inventory turnover for SKU A100",
        "When was order SO12345 fulfilled?",
        "Show me customer addresses",
        "RMA volume last month",
    ],
)
def test_does_not_detect_non_financial(query):
    assert is_financial_ambiguous(query) is False


def test_case_insensitive():
    assert is_financial_ambiguous("REVENUE this quarter") is True
    assert is_financial_ambiguous("revenue") is True


def test_word_boundary():
    """'revenuecycle' or 'gmv-something' shouldn't match — only standalone words."""
    assert is_financial_ambiguous("revenuecycle department") is False


def test_empty_string():
    assert is_financial_ambiguous("") is False


def test_none_safe():
    """Defensive: None-safe (orchestrator may pass None for blank turns)."""
    assert is_financial_ambiguous(None) is False  # type: ignore[arg-type]
