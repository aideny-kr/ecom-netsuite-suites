"""Tests for query eval harness — scoring query quality."""

import pytest

from app.services.query_eval_harness import (
    EvalCase,
    EvalScore,
    score_syntax,
    score_accuracy,
    score_efficiency,
    composite_score,
    load_eval_cases,
)


class TestScoreSyntax:
    def test_valid_select_scores_1(self):
        assert score_syntax("SELECT id FROM transaction", dialect="suiteql") == 1.0

    def test_valid_bigquery_scores_1(self):
        assert score_syntax("SELECT id FROM `project.dataset.table`", dialect="bigquery") == 1.0

    def test_uses_limit_in_suiteql_penalized(self):
        assert score_syntax("SELECT id FROM t LIMIT 10", dialect="suiteql") < 1.0

    def test_uses_fetch_first_in_bigquery_penalized(self):
        assert score_syntax("SELECT id FROM t FETCH FIRST 10 ROWS ONLY", dialect="bigquery") < 1.0

    def test_insert_scores_0(self):
        assert score_syntax("INSERT INTO t VALUES (1)", dialect="suiteql") == 0.0

    def test_empty_scores_0(self):
        assert score_syntax("", dialect="suiteql") == 0.0

    def test_current_date_in_suiteql_penalized(self):
        assert score_syntax("SELECT * FROM t WHERE d = CURRENT_DATE", dialect="suiteql") < 1.0

    def test_builtin_in_bigquery_penalized(self):
        assert score_syntax("SELECT BUILTIN.DF(status) FROM t", dialect="bigquery") < 1.0


class TestScoreAccuracy:
    def test_all_keywords_match(self):
        result = "Total revenue is $1.2M across 5 regions this quarter"
        expected = ["revenue", "region", "quarter"]
        assert score_accuracy(result, expected) >= 0.9

    def test_no_keywords_match(self):
        assert score_accuracy("Hello world", ["revenue", "region"]) == 0.0

    def test_partial_match(self):
        score = score_accuracy("Revenue was high", ["revenue", "region", "quarter"])
        assert 0.3 <= score <= 0.4

    def test_empty_result(self):
        assert score_accuracy("", ["revenue"]) == 0.0

    def test_empty_keywords(self):
        assert score_accuracy("some text", []) == 0.0


class TestScoreEfficiency:
    def test_select_star_penalized(self):
        assert score_efficiency("SELECT * FROM t") < 1.0

    def test_specific_columns_good(self):
        assert score_efficiency("SELECT id, name FROM t") >= 0.9

    def test_group_by_bonus(self):
        score = score_efficiency("SELECT dept, COUNT(*) FROM t GROUP BY dept")
        assert score >= 0.9

    def test_cte_bonus(self):
        score = score_efficiency("WITH cte AS (SELECT 1) SELECT * FROM cte")
        # Has SELECT * penalty but CTE bonus
        assert 0.5 < score < 1.0


class TestCompositeScore:
    def test_weighted_composite(self):
        score = composite_score(accuracy=0.9, syntax=1.0, efficiency=0.8)
        expected = 0.9 * 0.40 + 1.0 * 0.35 + 0.8 * 0.25  # 0.91
        assert abs(score - expected) < 0.01

    def test_all_perfect(self):
        assert composite_score(accuracy=1.0, syntax=1.0, efficiency=1.0) == 1.0

    def test_all_zero(self):
        assert composite_score(accuracy=0.0, syntax=0.0, efficiency=0.0) == 0.0


class TestLoadEvalCases:
    def test_load_suiteql_cases(self):
        cases = load_eval_cases("suiteql")
        assert len(cases) >= 10
        assert all(isinstance(c, EvalCase) for c in cases)
        assert all(c.dialect == "suiteql" for c in cases)

    def test_load_bigquery_cases(self):
        cases = load_eval_cases("bigquery")
        assert len(cases) >= 10
        assert all(c.dialect == "bigquery" for c in cases)

    def test_eval_case_has_fields(self):
        cases = load_eval_cases("suiteql")
        case = cases[0]
        assert case.question
        assert case.expected_keywords
        assert case.dialect == "suiteql"

    def test_nonexistent_dialect_returns_empty(self):
        assert load_eval_cases("nosql") == []
