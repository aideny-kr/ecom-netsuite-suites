# backend/tests/services/metrics/test_metric_authoring.py
import pytest

from app.services.metrics.metric_authoring import AuthoringError, validate_definition

_CROSS_SOURCE_KEYS = {
    "left_query",
    "left_dialect",
    "right_query",
    "right_dialect",
    "join_keys",
    "join_type",
    "select",
    "pivot",
}


def test_rejects_unknown_cross_source_key():
    with pytest.raises(AuthoringError):
        validate_definition(
            {
                "key": "x",
                "source_kind": "cross_source",
                "blessed_spec": {"left_query": "a", "right_query": "b", "join_keys": ["id"], "aggregations": "sum"},
            },
            allowed_cross_source_keys=_CROSS_SOURCE_KEYS,
        )


def test_rejects_expression_cycle():
    with pytest.raises(AuthoringError):
        validate_definition({"key": "a", "source_kind": "expression", "expression": "a / b", "depends_on": ["a", "b"]})


def test_rejects_both_spec_and_expression():
    with pytest.raises(AuthoringError):
        validate_definition(
            {"key": "x", "source_kind": "suiteql", "blessed_spec": {"query": "SELECT 1"}, "expression": "a/b"}
        )


def test_accepts_valid_expression():
    validate_definition(
        {
            "key": "net_margin",
            "source_kind": "expression",
            "expression": "net_income / gross_revenue",
            "depends_on": ["net_income", "gross_revenue"],
        }
    )
