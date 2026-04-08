"""Unit tests for classify_query_source_class()."""

from datetime import timedelta

import pytest

from app.services.chat.disclosure import (
    QueryClass,
    classify_query_source_class,
    compute_can_switch_source,
)


@pytest.mark.parametrize("query,expected", [
    # Dual-source (both)
    ("how many orders this week", QueryClass.DUAL_SOURCE),
    ("top customers by revenue", QueryClass.DUAL_SOURCE),
    ("items sold last month", QueryClass.DUAL_SOURCE),
    ("sales by channel", QueryClass.DUAL_SOURCE),
    ("transactions this quarter", QueryClass.DUAL_SOURCE),

    # NetSuite-only
    ("what's my balance sheet", QueryClass.NETSUITE_ONLY),
    ("income statement for Q1", QueryClass.NETSUITE_ONLY),
    ("GL journal entries last week", QueryClass.NETSUITE_ONLY),
    ("close the period", QueryClass.NETSUITE_ONLY),
    ("saved search for open invoices", QueryClass.NETSUITE_ONLY),
    ("show me the suitescript for RMA creation", QueryClass.NETSUITE_ONLY),

    # BigQuery-only
    ("total ad spend last month", QueryClass.BIGQUERY_ONLY),
    ("attribution by campaign", QueryClass.BIGQUERY_ONLY),
    ("funnel conversion rate", QueryClass.BIGQUERY_ONLY),
    ("session duration by source", QueryClass.BIGQUERY_ONLY),
    ("cohort retention for Q1", QueryClass.BIGQUERY_ONLY),

    # Unmatched
    ("hello", QueryClass.UNMATCHED),
    ("", QueryClass.UNMATCHED),
    ("how does the system work", QueryClass.UNMATCHED),
])
def test_classify_query_source_class(query, expected):
    assert classify_query_source_class(query) == expected


class _FakeConnectorState:
    """Minimal stand-in for the connector registry / connection_alerts lookups."""

    def __init__(self, has_bigquery=True, has_netsuite=True, bq_healthy=True, ns_healthy=True, bq_sync_age_hours=1):
        self.has_bigquery = has_bigquery
        self.has_netsuite = has_netsuite
        self.bq_healthy = bq_healthy
        self.ns_healthy = ns_healthy
        self.bq_sync_age = timedelta(hours=bq_sync_age_hours)


def test_can_switch_netsuite_to_bigquery_healthy_dual_source():
    state = _FakeConnectorState()
    assert compute_can_switch_source("netsuite", "orders query", state) is True


def test_can_switch_returns_false_for_netsuite_only_query():
    state = _FakeConnectorState()
    assert compute_can_switch_source("netsuite", "balance sheet last quarter", state) is False


def test_can_switch_returns_false_when_bigquery_not_connected():
    state = _FakeConnectorState(has_bigquery=False)
    assert compute_can_switch_source("netsuite", "orders this week", state) is False


def test_can_switch_returns_false_when_bigquery_stale():
    state = _FakeConnectorState(bq_sync_age_hours=9999)
    assert compute_can_switch_source("netsuite", "orders this week", state) is False


def test_can_switch_returns_false_when_bigquery_unhealthy():
    state = _FakeConnectorState(bq_healthy=False)
    assert compute_can_switch_source("netsuite", "orders this week", state) is False


def test_can_switch_returns_false_for_unmatched_query():
    state = _FakeConnectorState()
    assert compute_can_switch_source("netsuite", "hello", state) is False


def test_can_switch_bigquery_to_netsuite_healthy_dual_source():
    state = _FakeConnectorState()
    assert compute_can_switch_source("bigquery", "orders this week", state) is True


def test_can_switch_bigquery_returns_false_for_bigquery_only_query():
    state = _FakeConnectorState()
    assert compute_can_switch_source("bigquery", "ad spend last month", state) is False


def test_can_switch_bigquery_returns_false_when_netsuite_unhealthy():
    state = _FakeConnectorState(ns_healthy=False)
    assert compute_can_switch_source("bigquery", "orders this week", state) is False
