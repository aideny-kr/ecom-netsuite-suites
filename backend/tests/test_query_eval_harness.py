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

    # --- leak hardening (grill round 2) ---

    def test_country_filter_not_in_detected(self):
        sql = (
            "SELECT 1 FROM transactionShippingAddress sa "
            "WHERE BUILTIN.DF(sa.country) NOT IN ('SG','NO') "
            "AND t.trandate >= TO_DATE('2025-06-01','YYYY-MM-DD')"
        )
        assert "builtin_df_country_filter" in detect_perf_anti_patterns(sql)

    def test_country_filter_aliasless_detected(self):
        sql = (
            "SELECT 1 FROM transactionShippingAddress sa "
            "WHERE BUILTIN.DF(country) = 'Singapore' "
            "AND t.trandate >= TO_DATE('2025-06-01','YYYY-MM-DD')"
        )
        assert "builtin_df_country_filter" in detect_perf_anti_patterns(sql)

    def test_country_filter_reversed_comparison_detected(self):
        sql = (
            "SELECT 1 FROM transactionShippingAddress sa "
            "WHERE 'Singapore' = BUILTIN.DF(sa.country) "
            "AND t.trandate >= TO_DATE('2025-06-01','YYYY-MM-DD')"
        )
        assert "builtin_df_country_filter" in detect_perf_anti_patterns(sql)

    def test_country_suffix_field_not_flagged(self):
        # BUILTIN.DF(sa.shipcountry) is a different column — COUNTRY as a suffix must
        # not be mistaken for the country column (anchored on the '(' of the DF arg).
        sql = (
            "SELECT 1 FROM transactionShippingAddress sa "
            "WHERE BUILTIN.DF(sa.shipcountry) = 'X' "
            "AND t.trandate >= TO_DATE('2025-06-01','YYYY-MM-DD')"
        )
        assert "builtin_df_country_filter" not in detect_perf_anti_patterns(sql)

    def test_trandate_not_equal_is_not_a_bound(self):
        # `<>` (not-equal) does not bound the scan to a date range → still unbounded.
        sql = (
            "SELECT 1 FROM transactionShippingAddress sa "
            "JOIN transaction t ON t.shippingaddress = sa.nkey "
            "WHERE sa.country IN ('SG') AND t.trandate <> TO_DATE('2025-06-01','YYYY-MM-DD')"
        )
        assert "unbounded_address_join" in detect_perf_anti_patterns(sql)

    def test_lower_wrapped_country_filter_detected(self):
        # LOWER(BUILTIN.DF(sa.country)) = 'x' is an even worse per-row pattern (two
        # functions) and must still be flagged despite the wrapper paren.
        sql = (
            "SELECT 1 FROM transactionShippingAddress sa "
            "WHERE LOWER(BUILTIN.DF(sa.country)) = 'singapore' "
            "AND t.trandate >= TO_DATE('2025-06-01','YYYY-MM-DD')"
        )
        assert "builtin_df_country_filter" in detect_perf_anti_patterns(sql)

    def test_upper_wrapped_country_filter_in_detected(self):
        sql = (
            "SELECT 1 FROM transactionShippingAddress sa "
            "WHERE UPPER(BUILTIN.DF(sa.country)) IN ('SG','NO') "
            "AND t.trandate >= TO_DATE('2025-06-01','YYYY-MM-DD')"
        )
        assert "builtin_df_country_filter" in detect_perf_anti_patterns(sql)

    def test_commented_trandate_predicate_is_not_a_bound(self):
        # A trandate predicate inside a SQL comment must not count as a real scan bound.
        sql = (
            "SELECT 1 FROM transactionShippingAddress sa "
            "JOIN transaction t ON t.shippingaddress = sa.nkey "
            "WHERE sa.country IN ('SG')  -- t.trandate >= TO_DATE('2025-01-01','YYYY-MM-DD')"
        )
        assert "unbounded_address_join" in detect_perf_anti_patterns(sql)


class TestUnboundedDfLineScan:
    """A BUILTIN.DF filter in a transactionline query with no trandate range scans every line in
    the account's history. Measured on Framework 2026-10-05: the chat's Yucca daily count took
    65-116 s per query (7-minute turn) and timed out on the MCP; the same filter with a trandate
    floor returned the same rows in seconds. Rewriting it as an item subquery timed out too."""

    CHAT_QUERY = (
        "SELECT TRUNC(t.trandate) AS order_date, COUNT(DISTINCT t.id) AS orders FROM transaction t "
        "JOIN transactionline tl ON tl.transaction = t.id JOIN item i ON i.id = tl.item "
        "WHERE t.type = 'SalesOrd' AND BUILTIN.DF(i.custitem_fw_platform) = 'Yucca' AND tl.mainline = 'F' "
        "GROUP BY TRUNC(t.trandate) ORDER BY TRUNC(t.trandate)"
    )

    def test_the_chat_query_is_flagged(self):
        assert "unbounded_df_line_scan" in detect_perf_anti_patterns(self.CHAT_QUERY)

    def test_a_trandate_range_bounds_it(self):
        bounded = self.CHAT_QUERY.replace(
            "AND tl.mainline", "AND t.trandate >= TO_DATE('2026-09-01', 'YYYY-MM-DD') AND tl.mainline"
        )
        assert detect_perf_anti_patterns(bounded) == []

    def test_the_item_subquery_rewrite_is_flagged_too(self):
        sql = (
            "SELECT COUNT(DISTINCT t.id) FROM transaction t JOIN transactionline tl ON tl.transaction = t.id "
            "WHERE tl.item IN (SELECT i.id FROM item i WHERE BUILTIN.DF(i.custitem_fw_platform) = 'Yucca')"
        )
        assert "unbounded_df_line_scan" in detect_perf_anti_patterns(sql)

    def test_wrapped_and_reversed_filters_are_flagged(self):
        for predicate in (
            "UPPER(BUILTIN.DF(i.custitem_fw_platform)) LIKE '%YUCCA%'",
            "'Yucca' = BUILTIN.DF(i.custitem_fw_platform)",
            "BUILTIN.DF(t.status) IN ('Pending Fulfillment')",
        ):
            sql = f"SELECT t.id FROM transaction t JOIN transactionline tl ON tl.transaction = t.id WHERE {predicate}"
            assert "unbounded_df_line_scan" in detect_perf_anti_patterns(sql), predicate

    def test_display_only_df_and_raw_id_filters_are_not_flagged(self):
        sql = (
            "SELECT BUILTIN.DF(i.custitem_fw_platform) AS platform, COUNT(*) FROM transaction t "
            "JOIN transactionline tl ON tl.transaction = t.id JOIN item i ON i.id = tl.item "
            "WHERE i.custitem_fw_platform = 39 GROUP BY BUILTIN.DF(i.custitem_fw_platform)"
        )
        assert detect_perf_anti_patterns(sql) == []

    def test_df_filters_without_transaction_lines_are_not_flagged(self):
        sql = "SELECT i.id FROM item i WHERE UPPER(BUILTIN.DF(i.custitem_fw_platform)) LIKE '%YUCCA%'"
        assert detect_perf_anti_patterns(sql) == []

    def test_it_lowers_the_efficiency_score(self):
        assert score_efficiency(self.CHAT_QUERY) <= 0.75

    def test_a_case_label_in_the_select_list_is_not_a_filter(self):
        # #390 review R1: a display-only CASE compares BUILTIN.DF but never filters rows.
        sql = (
            "SELECT CASE WHEN BUILTIN.DF(i.custitem_fw_platform) = 'Yucca' THEN 'Yucca' ELSE 'Other' END AS p "
            "FROM transaction t JOIN transactionline tl ON tl.transaction = t.id JOIN item i ON i.id = tl.item "
            "WHERE t.id = 123"
        )
        assert detect_perf_anti_patterns(sql) == []

    def test_a_reversed_trandate_range_bounds_it(self):
        # #390 review R2: `TO_DATE(...) <= t.trandate` is the same range written the other way round.
        bounded = self.CHAT_QUERY.replace(
            "AND tl.mainline", "AND TO_DATE('2026-09-01', 'YYYY-MM-DD') <= t.trandate AND tl.mainline"
        )
        assert detect_perf_anti_patterns(bounded) == []
        # A not-equal comparison is still not a range.
        unbounded = self.CHAT_QUERY.replace("AND tl.mainline", "AND SYSDATE <> t.trandate AND tl.mainline")
        assert "unbounded_df_line_scan" in detect_perf_anti_patterns(unbounded)

    def test_a_two_argument_wrapper_is_flagged(self):
        # #390 review R3: NVL/COALESCE with a default must not slip past the check.
        sql = self.CHAT_QUERY.replace(
            "BUILTIN.DF(i.custitem_fw_platform) = 'Yucca'", "NVL(BUILTIN.DF(i.custitem_fw_platform), 'None') = 'Yucca'"
        )
        assert "unbounded_df_line_scan" in detect_perf_anti_patterns(sql)


class TestLiteralsAndCommentsCannotFoolTheCheck:
    """Quoted text, quoted identifiers and comments are hidden before any pattern is matched
    (#390 review round 2 R1-quoted/R3/R5, #397 round 3 R3). No structural parsing: #397 showed
    hand-written structure reading fails review round after round."""

    LINES = "FROM transaction t JOIN transactionline tl ON tl.transaction = t.id JOIN item i ON i.id = tl.item "

    def test_a_quoted_end_inside_a_label_does_not_expose_the_label(self):
        sql = (
            "SELECT CASE WHEN t.type = 'SalesOrd' THEN 'End' WHEN BUILTIN.DF(i.custitem_fw_platform) = 'Yucca' "
            "THEN 'Yucca' ELSE 'Other' END AS p " + self.LINES + "WHERE t.id = 123"
        )
        assert detect_perf_anti_patterns(sql) == []

    def test_an_escaped_quote_in_a_default_argument_does_not_hide_the_filter(self):
        sql = (
            "SELECT t.id " + self.LINES + "WHERE NVL(BUILTIN.DF(i.custitem_fw_platform), 'Doesn''t have one') = 'Yucca'"
        )
        assert "unbounded_df_line_scan" in detect_perf_anti_patterns(sql)

    def test_dashes_inside_a_value_are_not_a_comment(self):
        sql = (
            "SELECT t.id " + self.LINES + "WHERE BUILTIN.DF(i.custitem_fw_platform) = 'Yucca--EU' "
            "AND t.trandate >= TO_DATE('2026-09-01', 'YYYY-MM-DD')"
        )
        assert detect_perf_anti_patterns(sql) == []

    def test_a_real_comment_still_cannot_supply_the_range(self):
        sql = (
            "SELECT t.id " + self.LINES + "WHERE BUILTIN.DF(i.custitem_fw_platform) = 'Yucca' "
            "-- AND t.trandate >= TO_DATE('2026-09-01', 'YYYY-MM-DD')\n"
        )
        assert "unbounded_df_line_scan" in detect_perf_anti_patterns(sql)

    def test_a_quoted_identifier_is_opaque(self):
        sql = 'SELECT BUILTIN.DF(tl.location) AS "Ship -- From" ' + self.LINES + "WHERE BUILTIN.DF(tl.item) = 'Widget'"
        assert "unbounded_df_line_scan" in detect_perf_anti_patterns(sql)


class TestARangeNeedsALowerLimit:
    """`t.trandate <= today` alone still reads all history: vs-MCP 2026-10-06, sales_country_canonical
    read "as of today" that way and timed out on both sides (54-60 s per query)."""

    DF_SCAN = (
        "SELECT t.id FROM transaction t JOIN transactionline tl ON tl.transaction = t.id JOIN item i ON i.id = tl.item "
        "WHERE BUILTIN.DF(i.custitem_fw_platform) = 'Yucca' AND {bound}"
    )

    def test_upper_limits_alone_are_not_a_range(self):
        for bound in (
            "t.trandate <= TO_DATE('2026-10-06', 'YYYY-MM-DD')",
            "t.trandate < SYSDATE",
            "TO_DATE('2026-10-06', 'YYYY-MM-DD') >= t.trandate",
            "SYSDATE > t.trandate",
        ):
            assert "unbounded_df_line_scan" in detect_perf_anti_patterns(self.DF_SCAN.format(bound=bound)), bound

    def test_lower_limits_single_days_and_between_are_ranges(self):
        for bound in (
            "t.trandate >= TO_DATE('2026-09-01', 'YYYY-MM-DD')",
            "t.trandate > SYSDATE - 30",
            "TRUNC(t.trandate) = TRUNC(SYSDATE)",
            "t.trandate BETWEEN TO_DATE('2026-09-01', 'YYYY-MM-DD') AND SYSDATE",
            "TO_DATE('2026-09-01', 'YYYY-MM-DD') <= t.trandate",
            "SYSDATE - 7 < t.trandate",
        ):
            assert detect_perf_anti_patterns(self.DF_SCAN.format(bound=bound)) == [], bound

    def test_the_benchmark_address_query_scores_as_unbounded(self):
        sql = (
            "SELECT BUILTIN.DF(sa.country) AS ship_country, COUNT(DISTINCT t.id) FROM transaction t "
            "JOIN transactionShippingAddress sa ON sa.nKey = t.shippingAddress WHERE t.type = 'SalesOrd' "
            "AND t.trandate <= TO_DATE('2026-10-06', 'YYYY-MM-DD') AND sa.country IN ('NO', 'CH') "
            "GROUP BY BUILTIN.DF(sa.country)"
        )
        assert "unbounded_address_join" in detect_perf_anti_patterns(sql)


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
