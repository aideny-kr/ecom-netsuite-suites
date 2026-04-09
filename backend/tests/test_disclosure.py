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
