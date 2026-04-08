"""Unit tests for SOURCE_SWITCH_RE and PUSHBACK_RE."""

import pytest

from app.services.chat.disclosure import (
    PUSHBACK_RE,
    SOURCE_SWITCH_RE,  # noqa: F401  -- imported to assert the symbol exists
    parse_source_switch,
)

# ── SOURCE_SWITCH_RE positive cases ──────────────────────────────────────


@pytest.mark.parametrize(
    "message,expected",
    [
        ("use BigQuery", "bigquery"),
        ("use bigquery", "bigquery"),
        ("USE BIGQUERY", "bigquery"),
        ("use netsuite", "netsuite"),
        ("switch to BigQuery", "bigquery"),
        ("switch to netsuite", "netsuite"),
        ("run on BigQuery", "bigquery"),
        ("try BigQuery", "bigquery"),
        ("use bq", "bigquery"),
        ("use ns", "netsuite"),
        ("  use BigQuery  ", "bigquery"),
        ("use BigQuery.", "bigquery"),
        ("use BigQuery!", "bigquery"),
    ],
)
def test_source_switch_positive(message, expected):
    assert parse_source_switch(message) == expected


# ── SOURCE_SWITCH_RE negative cases ──────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        ("let me use BigQuery to find the answer"),  # not anchored to start
        ("I would use BigQuery for this"),
        ("use BigQuery for the marketing data"),  # trailing words
        ("use spreadsheet"),  # wrong target
        ("use"),  # no target
        (""),  # empty
        ("bigquery"),  # no verb
        ("switch"),  # no target
        ("use BigQuery and NetSuite"),  # trailing clause
    ],
)
def test_source_switch_negative(message):
    assert parse_source_switch(message) is None


# ── PUSHBACK_RE ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "that's wrong",
        "that is wrong",
        "thats not right",
        "no, I meant fiscal Q1",
        "no I meant the US subsidiary",
        "actually, show me last week",
        "why is that different from BigQuery",
        "I need recognized revenue, not billed",
    ],
)
def test_pushback_positive(message):
    assert PUSHBACK_RE.match(message) is not None


@pytest.mark.parametrize(
    "message",
    [
        "that's helpful",
        "can you also show the breakdown",
        "show me more",
        "how does this work",
        "what about last month",
    ],
)
def test_pushback_negative(message):
    assert PUSHBACK_RE.match(message) is None
