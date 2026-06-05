"""Tests for query eval harness — scoring query quality."""

from app.services.query_eval_harness import (
    EvalCase,
    composite_score,
    detect_perf_anti_patterns,
    load_eval_cases,
    score_accuracy,
    score_efficiency,
    score_syntax,
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

    def test_builtin_df_country_filter_penalized(self):
        # BUILTIN.DF(<addr>.country) used as a FILTER is a per-row function → full
        # scan → timeout. Date-scoped here so ONLY the country-filter penalty fires.
        slow = score_efficiency(
            "SELECT i.itemid FROM transactionShippingAddress sa "
            "JOIN transaction t ON t.shippingaddress = sa.nkey "
            "JOIN transactionline tl ON tl.transaction = t.id "
            "JOIN item i ON i.id = tl.item "
            "WHERE BUILTIN.DF(sa.country) IN ('Singapore','Norway') "
            "AND t.trandate >= TO_DATE('2025-06-01','YYYY-MM-DD')"
        )
        assert slow < 1.0

    def test_builtin_df_small_list_filter_not_penalized(self):
        # BUILTIN.DF(field) = 'Value' on a small static custom list is a blessed
        # readability pattern (netsuite.yaml CUSTOM LIST FIELDS), NOT a perf killer.
        # The penalty is scoped to address-country filters only — this must NOT trip.
        ok = score_efficiency("SELECT i.itemid FROM item i WHERE BUILTIN.DF(i.custitem_fw_platform) = 'Laptop 13'")
        assert ok >= 0.9

    def test_builtin_df_in_select_only_not_penalized(self):
        # BUILTIN.DF in the SELECT list (for display) is fine; filter is on the raw value.
        ok = score_efficiency(
            "SELECT BUILTIN.DF(sa.country) AS country FROM transactionShippingAddress sa "
            "WHERE sa.country = 'SG' AND t.trandate >= TO_DATE('2025-01-01','YYYY-MM-DD')"
        )
        assert ok >= 0.9

    def test_unbounded_address_join_penalized(self):
        # Address-table join with no trandate / ROWNUM / FETCH bound → times out unbounded.
        unbounded = score_efficiency(
            "SELECT i.itemid FROM transactionShippingAddress sa "
            "JOIN transaction t ON t.shippingaddress = sa.nkey "
            "JOIN transactionline tl ON tl.transaction = t.id "
            "JOIN item i ON i.id = tl.item WHERE sa.country IN ('SG','NZ')"
        )
        assert unbounded < 1.0

    def test_bounded_address_join_ok(self):
        bounded = score_efficiency(
            "SELECT i.itemid FROM transactionShippingAddress sa "
            "JOIN transaction t ON t.shippingaddress = sa.nkey "
            "JOIN transactionline tl ON tl.transaction = t.id "
            "JOIN item i ON i.id = tl.item WHERE sa.country IN ('SG','NZ') "
            "AND t.trandate >= TO_DATE('2025-06-01','YYYY-MM-DD') FETCH FIRST 500 ROWS ONLY"
        )
        assert bounded >= 0.9

    def test_address_join_with_trandate_in_select_only_still_penalized(self):
        # trandate is SELECTed / ORDER BY'd but is NOT a filter predicate — still unbounded.
        s = score_efficiency(
            "SELECT t.trandate, i.itemid FROM transactionShippingAddress sa "
            "JOIN transaction t ON t.shippingaddress = sa.nkey "
            "JOIN transactionline tl ON tl.transaction = t.id "
            "JOIN item i ON i.id = tl.item WHERE sa.country IN ('SG') ORDER BY t.trandate"
        )
        assert s < 1.0

    def test_fetch_first_alone_is_not_a_scope(self):
        # FETCH FIRST limits returned rows, NOT the scan — an all-time address join
        # with only FETCH FIRST still full-scans → must stay penalized (needs trandate).
        s = score_efficiency(
            "SELECT i.itemid FROM transactionShippingAddress sa "
            "JOIN transaction t ON t.shippingaddress = sa.nkey "
            "JOIN item i ON i.id = tl.item WHERE sa.country IN ('SG') "
            "FETCH FIRST 500 ROWS ONLY"
        )
        assert s < 1.0

    def test_trunc_trandate_predicate_counts_as_bound(self):
        # TRUNC(t.trandate) >= ... is a real date bound — the ')' before >= must not
        # hide the predicate (closes the regex false-negative).
        bounded = score_efficiency(
            "SELECT i.itemid FROM transactionShippingAddress sa "
            "JOIN transaction t ON t.shippingaddress = sa.nkey "
            "JOIN item i ON i.id = tl.item WHERE sa.country IN ('SG') "
            "AND TRUNC(t.trandate) >= TRUNC(SYSDATE) - 365"
        )
        assert bounded >= 0.9


class TestDetectPerfAntiPatterns:
    def test_country_filter_detected(self):
        sql = (
            "SELECT 1 FROM transactionShippingAddress sa "
            "WHERE BUILTIN.DF(sa.country) = 'Singapore' "
            "AND t.trandate >= TO_DATE('2025-06-01','YYYY-MM-DD')"
        )
        assert "builtin_df_country_filter" in detect_perf_anti_patterns(sql)

    def test_unbounded_address_join_detected(self):
        sql = (
            "SELECT 1 FROM transactionShippingAddress sa "
            "JOIN transaction t ON t.shippingaddress = sa.nkey WHERE sa.country = 'SG'"
        )
        assert "unbounded_address_join" in detect_perf_anti_patterns(sql)

    def test_both_detected(self):
        sql = "SELECT 1 FROM transactionShippingAddress sa WHERE BUILTIN.DF(sa.country) IN ('SG','NO')"
        reasons = detect_perf_anti_patterns(sql)
        assert "builtin_df_country_filter" in reasons
        assert "unbounded_address_join" in reasons

    def test_display_use_not_flagged(self):
        sql = (
            "SELECT BUILTIN.DF(sa.country) AS country FROM transactionShippingAddress sa "
            "WHERE sa.country = 'SG' AND t.trandate >= TO_DATE('2025-06-01','YYYY-MM-DD') "
            "GROUP BY BUILTIN.DF(sa.country)"
        )
        assert detect_perf_anti_patterns(sql) == []

    def test_clean_query_no_patterns(self):
        assert detect_perf_anti_patterns("SELECT COUNT(*) FROM transaction WHERE type = 'SalesOrd'") == []

    def test_small_list_builtin_df_not_flagged(self):
        # non-country BUILTIN.DF filter (small custom list) is not an address/country
        # perf pattern — must not be flagged (reconciles with netsuite.yaml line 52).
        sql = "SELECT i.itemid FROM item i WHERE BUILTIN.DF(i.custitem_fw_platform) = 'Laptop 13'"
        assert detect_perf_anti_patterns(sql) == []

    def test_billing_address_country_filter_detected(self):
        sql = "SELECT 1 FROM transactionBillingAddress ba WHERE BUILTIN.DF(ba.country) = 'US'"
        reasons = detect_perf_anti_patterns(sql)
        assert "builtin_df_country_filter" in reasons
        assert "unbounded_address_join" in reasons

    def test_empty_sql_no_patterns(self):
        assert detect_perf_anti_patterns("") == []


class TestCompositeScore:
    def test_weighted_composite(self):
        # Weights: accuracy 30%, syntax 30%, efficiency 15%, sql_match 25%
        score = composite_score(accuracy=0.9, syntax=1.0, efficiency=0.8)
        expected = 0.9 * 0.30 + 1.0 * 0.30 + 0.8 * 0.15 + 0.0 * 0.25  # 0.69
        assert abs(score - expected) < 0.01

    def test_all_perfect(self):
        assert composite_score(accuracy=1.0, syntax=1.0, efficiency=1.0, sql_match=1.0) == 1.0

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
