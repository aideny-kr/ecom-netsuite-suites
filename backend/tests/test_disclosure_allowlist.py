"""Unit tests for classify_query_source_class()."""

import pytest

from app.services.chat.disclosure import QueryClass, classify_query_source_class


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
