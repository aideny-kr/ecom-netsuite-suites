# backend/tests/services/metrics/test_expression_evaluator.py
import pytest

from app.services.metrics.expression_evaluator import (
    ExpressionError,
    evaluate_expression,
    extract_dependencies,
)


def test_extract_dependencies():
    assert sorted(extract_dependencies("net_income / gross_revenue")) == ["gross_revenue", "net_income"]


def test_evaluate_basic():
    assert evaluate_expression("net_income / gross_revenue", {"net_income": 30.0, "gross_revenue": 120.0}) == 0.25


def test_division_by_zero_raises():
    with pytest.raises(ExpressionError):
        evaluate_expression("a / b", {"a": 1.0, "b": 0.0})


def test_rejects_non_whitelisted_tokens():
    with pytest.raises(ExpressionError):
        evaluate_expression("__import__('os')", {})


def test_missing_dependency_raises():
    with pytest.raises(ExpressionError):
        evaluate_expression("a / b", {"a": 1.0})
