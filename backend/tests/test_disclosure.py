"""Tests for the disclosure module: DisclosureBlock dataclass, WHERE-clause parser,
query classifier, can_switch_source logic, and assemble_disclosure hook.
"""

from __future__ import annotations


class TestDisclosureBlock:
    def test_defaults(self):
        from app.services.chat.disclosure import DisclosureBlock

        block = DisclosureBlock(source="netsuite", interpretation="")
        assert block.source == "netsuite"
        assert block.interpretation == ""
        assert block.implicit_filters == []
        assert block.can_switch_source is False
        assert block.is_rerun is False
        assert block.failure_mode is False

    def test_to_dict_full(self):
        from app.services.chat.disclosure import DisclosureBlock

        block = DisclosureBlock(
            source="bigquery",
            interpretation='"This week" = Mon Apr 5 - today',
            implicit_filters=["Excludes cancelled orders", "Excludes test orders"],
            can_switch_source=True,
            is_rerun=True,
            failure_mode=False,
        )
        d = block.to_dict()
        assert d == {
            "source": "bigquery",
            "interpretation": '"This week" = Mon Apr 5 - today',
            "implicit_filters": ["Excludes cancelled orders", "Excludes test orders"],
            "can_switch_source": True,
            "is_rerun": True,
            "failure_mode": False,
        }

    def test_to_dict_defaults(self):
        from app.services.chat.disclosure import DisclosureBlock

        block = DisclosureBlock(source="netsuite", interpretation="x")
        d = block.to_dict()
        assert d["source"] == "netsuite"
        assert d["interpretation"] == "x"
        assert d["implicit_filters"] == []
        assert d["can_switch_source"] is False
        assert d["is_rerun"] is False
        assert d["failure_mode"] is False

    def test_source_literal_type(self):
        """Source must be literal netsuite | bigquery."""
        from app.services.chat.disclosure import DisclosureBlock

        # Valid
        DisclosureBlock(source="netsuite", interpretation="")
        DisclosureBlock(source="bigquery", interpretation="")


class TestParseWhereClause:
    def _parse(self, sql: str):
        from app.services.chat.disclosure import parse_where_clause

        return parse_where_clause(sql)

    def test_empty_sql(self):
        result = self._parse("")
        assert result.interpretation == ""
        assert result.filters == []

    def test_no_where_clause(self):
        result = self._parse("SELECT * FROM transaction")
        assert result.interpretation == ""
        assert result.filters == []

    def test_date_range_explicit(self):
        sql = (
            "SELECT COUNT(*) FROM transaction "
            "WHERE trandate >= TO_DATE('2026-04-01', 'YYYY-MM-DD') "
            "AND trandate <= TO_DATE('2026-04-07', 'YYYY-MM-DD')"
        )
        result = self._parse(sql)
        assert "2026-04-01" in result.interpretation
        assert "2026-04-07" in result.interpretation

    def test_relative_date_week(self):
        sql = "SELECT COUNT(*) FROM transaction WHERE trandate >= TRUNC(SYSDATE, 'WW')"
        result = self._parse(sql)
        assert "week" in result.interpretation.lower()

    def test_relative_date_month(self):
        sql = "SELECT COUNT(*) FROM transaction WHERE trandate >= TRUNC(SYSDATE, 'MM')"
        result = self._parse(sql)
        assert "month" in result.interpretation.lower()

    def test_type_equality_sales_order(self):
        sql = "SELECT * FROM transaction WHERE type = 'SalesOrd' AND trandate >= TRUNC(SYSDATE, 'WW')"
        result = self._parse(sql)
        assert (
            any("sales order" in f.lower() or "salesord" in f.lower() for f in result.filters)
            or "order" in result.interpretation.lower()
        )

    def test_type_in_list(self):
        sql = "SELECT * FROM transaction WHERE type IN ('SalesOrd', 'CashSale')"
        result = self._parse(sql)
        assert len(result.filters) + len(result.interpretation) > 0  # something parsed

    def test_status_equality(self):
        sql = "SELECT * FROM transaction WHERE status = 'B'"
        result = self._parse(sql)
        assert any("status" in f.lower() or "posted" in f.lower() for f in result.filters)

    def test_status_in_list(self):
        sql = "SELECT * FROM transaction WHERE status IN ('A','B','C')"
        result = self._parse(sql)
        assert any("status" in f.lower() for f in result.filters)

    def test_subsidiary(self):
        sql = "SELECT * FROM transaction WHERE subsidiary = 5"
        result = self._parse(sql)
        assert any("subsidiary" in f.lower() for f in result.filters)

    def test_boolean_false(self):
        sql = "SELECT * FROM transaction WHERE is_test = false"
        result = self._parse(sql)
        assert any("test" in f.lower() for f in result.filters)

    def test_cancelled_is_null(self):
        sql = "SELECT * FROM transaction WHERE cancelled_at IS NULL"
        result = self._parse(sql)
        assert any("cancel" in f.lower() for f in result.filters)

    def test_currency(self):
        sql = "SELECT * FROM transaction WHERE currency = 1"
        result = self._parse(sql)
        assert any("currency" in f.lower() for f in result.filters)

    def test_entity_equality(self):
        sql = "SELECT * FROM transaction WHERE entity = 12345"
        result = self._parse(sql)
        assert any("entity" in f.lower() or "customer" in f.lower() for f in result.filters)

    def test_bigquery_table_suffix(self):
        sql = "SELECT * FROM `project.dataset.events_*` WHERE _TABLE_SUFFIX BETWEEN '20260401' AND '20260407'"
        result = self._parse(sql)
        assert "2026-04-01" in result.interpretation or "20260401" in result.interpretation

    def test_fetch_first(self):
        sql = "SELECT * FROM transaction FETCH FIRST 100 ROWS ONLY"
        result = self._parse(sql)
        assert any("100" in f for f in result.filters)

    def test_bigquery_limit(self):
        sql = "SELECT * FROM `project.dataset.table` LIMIT 50"
        result = self._parse(sql)
        assert any("50" in f for f in result.filters)


class TestParseWhereClauseGracefulDegrade:
    """Exotic / unparseable predicates should silently drop, not crash."""

    def _parse(self, sql: str):
        from app.services.chat.disclosure import parse_where_clause

        return parse_where_clause(sql)

    def test_nested_subquery_skipped(self):
        sql = "SELECT * FROM transaction WHERE id IN (SELECT id FROM foo WHERE bar > 10)"
        result = self._parse(sql)
        # should not crash, may return empty
        assert isinstance(result.interpretation, str)
        assert isinstance(result.filters, list)

    def test_window_function_ignored(self):
        sql = "SELECT ROW_NUMBER() OVER (PARTITION BY id) FROM transaction WHERE trandate >= TRUNC(SYSDATE, 'WW')"
        result = self._parse(sql)
        # date predicate still parsed
        assert "week" in result.interpretation.lower()

    def test_custom_record_field(self):
        sql = "SELECT * FROM customrecord_x WHERE custrecord_weird_field = 'foo'"
        result = self._parse(sql)
        # exotic predicate drops silently
        assert isinstance(result.filters, list)

    def test_malformed_sql(self):
        sql = "NOT VALID SQL AT ALL {{ template }}"
        result = self._parse(sql)
        assert result.interpretation == ""
        assert result.filters == []

    def test_unicode_and_case(self):
        sql = "SELECT * FROM transaction WHERE TRANDATE >= trunc(sysdate, 'ww')"
        result = self._parse(sql)
        assert "week" in result.interpretation.lower()


import pytest  # noqa: E402


class TestClassifyQueryClass:
    def _classify(self, q: str) -> str:
        from app.services.chat.disclosure import classify_query_class

        return classify_query_class(q)

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("how many orders this week", "orders"),
            ("top 10 customers by sales", "orders"),
            ("total transactions last month", "orders"),
            ("show item 12345", "orders"),
            ("journal entries for March", "gl"),
            ("gl balance for period", "gl"),
            ("close the month", "gl"),
            ("period end accruals", "gl"),
            ("ad spend by channel", "marketing"),
            ("attribution for last quarter", "marketing"),
            ("marketing sessions this week", "marketing"),
            ("cohort retention by month", "marketing"),
            ("saved search named open_invoices", "saved_search"),
            ("run the custom record query", "saved_search"),
            ("how does the suitescript work", "saved_search"),
            ("what's the weather", "unmatched"),
            ("tell me a joke", "unmatched"),
            ("hi", "unmatched"),
            ("explain revenue recognition", "unmatched"),
            ("account summary", "unmatched"),
        ],
    )
    def test_classify(self, query, expected):
        assert self._classify(query) == expected


class TestCanSwitchSource:
    def test_allowed_orders_bigquery_healthy(self, monkeypatch):
        from app.services.chat import disclosure

        monkeypatch.setattr(disclosure, "_tenant_has_connector", lambda tid, src: True)
        monkeypatch.setattr(disclosure, "_connector_is_healthy", lambda tid, src: True)
        monkeypatch.setattr(disclosure, "_bigquery_sync_age_hours", lambda tid: 2)

        from uuid import uuid4

        assert disclosure.compute_can_switch_source("netsuite", uuid4(), "orders") is True

    def test_blocked_gl_netsuite_only(self, monkeypatch):
        from app.services.chat import disclosure

        monkeypatch.setattr(disclosure, "_tenant_has_connector", lambda tid, src: True)
        monkeypatch.setattr(disclosure, "_connector_is_healthy", lambda tid, src: True)
        monkeypatch.setattr(disclosure, "_bigquery_sync_age_hours", lambda tid: 2)

        from uuid import uuid4

        assert disclosure.compute_can_switch_source("netsuite", uuid4(), "gl") is False

    def test_blocked_marketing_bigquery_only(self, monkeypatch):
        from app.services.chat import disclosure

        monkeypatch.setattr(disclosure, "_tenant_has_connector", lambda tid, src: True)
        monkeypatch.setattr(disclosure, "_connector_is_healthy", lambda tid, src: True)
        monkeypatch.setattr(disclosure, "_bigquery_sync_age_hours", lambda tid: 2)

        from uuid import uuid4

        assert disclosure.compute_can_switch_source("bigquery", uuid4(), "marketing") is False

    def test_blocked_missing_connector(self, monkeypatch):
        from app.services.chat import disclosure

        monkeypatch.setattr(disclosure, "_tenant_has_connector", lambda tid, src: False)
        monkeypatch.setattr(disclosure, "_connector_is_healthy", lambda tid, src: True)
        monkeypatch.setattr(disclosure, "_bigquery_sync_age_hours", lambda tid: 2)

        from uuid import uuid4

        assert disclosure.compute_can_switch_source("netsuite", uuid4(), "orders") is False

    def test_blocked_unhealthy_connector(self, monkeypatch):
        from app.services.chat import disclosure

        monkeypatch.setattr(disclosure, "_tenant_has_connector", lambda tid, src: True)
        monkeypatch.setattr(disclosure, "_connector_is_healthy", lambda tid, src: False)
        monkeypatch.setattr(disclosure, "_bigquery_sync_age_hours", lambda tid: 2)

        from uuid import uuid4

        assert disclosure.compute_can_switch_source("netsuite", uuid4(), "orders") is False

    def test_blocked_bigquery_stale(self, monkeypatch):
        from app.services.chat import disclosure

        monkeypatch.setattr(disclosure, "_tenant_has_connector", lambda tid, src: True)
        monkeypatch.setattr(disclosure, "_connector_is_healthy", lambda tid, src: True)
        monkeypatch.setattr(disclosure, "_bigquery_sync_age_hours", lambda tid: 48)

        from uuid import uuid4

        assert disclosure.compute_can_switch_source("netsuite", uuid4(), "orders") is False

    def test_unmatched_query_class_returns_false(self, monkeypatch):
        from app.services.chat import disclosure

        monkeypatch.setattr(disclosure, "_tenant_has_connector", lambda tid, src: True)
        monkeypatch.setattr(disclosure, "_connector_is_healthy", lambda tid, src: True)
        monkeypatch.setattr(disclosure, "_bigquery_sync_age_hours", lambda tid: 2)

        from uuid import uuid4

        assert disclosure.compute_can_switch_source("netsuite", uuid4(), "unmatched") is False


class TestSourceSwitchRegex:
    @pytest.mark.parametrize(
        "msg,target",
        [
            ("use BigQuery", "bigquery"),
            ("Use NetSuite", "netsuite"),
            ("switch to bigquery", "bigquery"),
            ("switch to NS", "netsuite"),
            ("run on bq", "bigquery"),
            ("try netsuite", "netsuite"),
            ("use BigQuery.", "bigquery"),
            ("  use  bigquery  ", "bigquery"),
            ("use bq!", "bigquery"),
        ],
    )
    def test_positive(self, msg, target):
        from app.services.chat.disclosure import _SOURCE_ALIASES, _SOURCE_SWITCH_RE

        m = _SOURCE_SWITCH_RE.match(msg)
        assert m is not None
        raw = m.group(1).lower()
        resolved = _SOURCE_ALIASES.get(raw, raw)
        assert resolved == target

    @pytest.mark.parametrize(
        "msg",
        [
            "can I use BigQuery for this?",
            "what about netsuite",
            "use BigQuery for orders and then netsuite for gl",
            "I already use BigQuery daily",
            "try comparing netsuite and bigquery",
            "",
        ],
    )
    def test_negative(self, msg):
        from app.services.chat.disclosure import _SOURCE_SWITCH_RE

        assert _SOURCE_SWITCH_RE.match(msg) is None


class TestPushbackRegex:
    @pytest.mark.parametrize(
        "msg",
        [
            "that's wrong",
            "thats wrong",
            "That's not right",
            "no, I meant last week",
            "actually it should be this week",
            "why is that filter there",
            "I need only sales orders",
            "actually, include cancelled",
            "no i meant customers",
            "why is subsidiary 5 included",
        ],
    )
    def test_positive(self, msg):
        from app.services.chat.disclosure import _PUSHBACK_RE

        assert _PUSHBACK_RE.match(msg) is not None

    @pytest.mark.parametrize(
        "msg",
        [
            "thanks, that works",
            "great, can you add a chart",
            "perfect",
            "ok now show me customers",
            "how about last month",
            "run the same query for q1",
            "export to csv",
            "compare with last year",
            "show top 10",
            "add a filter for subsidiary",
        ],
    )
    def test_negative(self, msg):
        from app.services.chat.disclosure import _PUSHBACK_RE

        assert _PUSHBACK_RE.match(msg) is None
