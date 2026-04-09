"""Unit tests for the best-effort WHERE-clause parser.

Parser covers ~10 common predicate shapes; everything else is silently
dropped (graceful degrade). We intentionally do NOT build a full SQL AST.
"""

from app.services.chat.disclosure import ParsedFilters, parse_where_clause

# ── Temporal predicates ──────────────────────────────────────────────────


def test_date_range_literal():
    sql = "SELECT * FROM transaction WHERE trandate >= '2026-01-01' AND trandate <= '2026-01-31'"
    filters = parse_where_clause(sql)
    assert "2026-01-01" in filters.interpretation
    assert "2026-01-31" in filters.interpretation


def test_relative_week():
    sql = "SELECT * FROM transaction WHERE trandate >= TRUNC(SYSDATE, 'WW')"
    filters = parse_where_clause(sql)
    assert "this week" in filters.interpretation.lower()


def test_relative_month():
    sql = "SELECT * FROM transaction WHERE trandate >= TRUNC(SYSDATE, 'MM')"
    filters = parse_where_clause(sql)
    assert "this month" in filters.interpretation.lower()


def test_relative_quarter():
    sql = "SELECT * FROM transaction WHERE trandate >= TRUNC(SYSDATE, 'Q')"
    filters = parse_where_clause(sql)
    assert "this quarter" in filters.interpretation.lower()


def test_relative_year():
    sql = "SELECT * FROM transaction WHERE trandate >= TRUNC(SYSDATE, 'YYYY')"
    filters = parse_where_clause(sql)
    assert "this year" in filters.interpretation.lower()


# ── Type / status predicates ────────────────────────────────────────────


def test_transaction_type_single():
    sql = "SELECT * FROM transaction WHERE type = 'SalesOrd'"
    filters = parse_where_clause(sql)
    assert any("sales order" in f.lower() for f in filters.implicit_filters)


def test_status_in():
    sql = "SELECT * FROM transaction WHERE status IN ('B', 'H')"
    filters = parse_where_clause(sql)
    assert any("status" in f.lower() for f in filters.implicit_filters)


# ── Scope / boolean predicates ───────────────────────────────────────────


def test_subsidiary_equality():
    sql = "SELECT * FROM transaction WHERE subsidiary = 5"
    filters = parse_where_clause(sql)
    assert any("subsidiary" in f.lower() for f in filters.implicit_filters)


def test_boolean_flag_false():
    sql = "SELECT * FROM orders WHERE is_test = false AND cancelled_at IS NULL"
    filters = parse_where_clause(sql)
    assert any("test" in f.lower() for f in filters.implicit_filters)
    assert any("cancelled" in f.lower() for f in filters.implicit_filters)


def test_combined_predicates_in_one_where():
    """Verify the parser extracts ALL applicable predicates from a single WHERE clause."""
    sql = (
        "SELECT * FROM transaction "
        "WHERE trandate >= TRUNC(SYSDATE, 'MM') "
        "AND type = 'SalesOrd' "
        "AND status IN ('B','H') "
        "AND subsidiary = 5"
    )
    filters = parse_where_clause(sql)
    assert "month" in filters.interpretation.lower()
    assert any("Sales Order" in f for f in filters.implicit_filters)
    assert any("Status" in f and "B" in f and "H" in f for f in filters.implicit_filters)
    assert any("Subsidiary ID 5" in f for f in filters.implicit_filters)


# ── BigQuery dialect ─────────────────────────────────────────────────────


def test_bigquery_limit():
    sql = "SELECT * FROM `project.dataset.orders` WHERE DATE(created_at) >= CURRENT_DATE() LIMIT 100"
    filters = parse_where_clause(sql)
    # Should not crash; at minimum returns empty filters
    assert isinstance(filters, ParsedFilters)


def test_bigquery_orderdate_range():
    """BigQuery date columns aren't named `trandate` — they're `orderdate`,
    `posting_date`, `created_at`, etc. The parser must recognize the family."""
    sql = (
        "SELECT customer, SUM(total) FROM `proj.ds.orders` "
        "WHERE orderdate >= '2026-01-01' AND orderdate <= '2026-12-31' "
        "GROUP BY 1"
    )
    filters = parse_where_clause(sql)
    assert "2026-01-01" in filters.interpretation
    assert "2026-12-31" in filters.interpretation


def test_bigquery_orderstatus_in():
    """BigQuery uses `orderstatus`, not bare `status`. The parser should match."""
    sql = "SELECT * FROM `proj.ds.orders` WHERE orderstatus NOT IN ('Cancelled', 'Voided', 'Closed')"
    filters = parse_where_clause(sql)
    assert any("status" in f.lower() for f in filters.implicit_filters)
    assert any("Cancelled" in f for f in filters.implicit_filters)


def test_bigquery_half_open_range_to_current_date():
    """Common BigQuery pattern: `>= literal AND <= CURRENT_DATE()`. Should
    render as 'YYYY-MM-DD – today'."""
    sql = "SELECT * FROM `proj.ds.orders` WHERE orderdate >= '2026-01-01' AND orderdate <= CURRENT_DATE()"
    filters = parse_where_clause(sql)
    assert "2026-01-01" in filters.interpretation
    assert "today" in filters.interpretation.lower()


def test_bigquery_lower_bound_only_falls_back_to_since():
    """If only the lower bound is literal and no upper bound is present at all."""
    sql = "SELECT * FROM `proj.ds.orders` WHERE orderdate >= '2025-01-01'"
    filters = parse_where_clause(sql)
    assert "Since 2025-01-01" in filters.interpretation


def test_bigquery_combined_predicates():
    """End-to-end check: a real BigQuery query like the bi-agent produced
    locally — orderdate range + orderstatus exclusion. Both should appear."""
    sql = (
        "SELECT customer, SUM(total) FROM `proj.ds.orders` "
        "WHERE orderstatus NOT IN ('Cancelled', 'Voided', 'Closed') "
        "AND orderdate >= '2026-01-01' "
        "AND orderdate <= '2026-12-31' "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
    )
    filters = parse_where_clause(sql)
    assert "2026-01-01" in filters.interpretation
    assert "2026-12-31" in filters.interpretation
    assert any("status" in f.lower() for f in filters.implicit_filters)


# ── Graceful degrade ─────────────────────────────────────────────────────


def test_exotic_subquery_does_not_crash():
    sql = """
        WITH recent AS (SELECT id FROM transaction WHERE trandate > SYSDATE - 7)
        SELECT * FROM transaction t
        WHERE t.id IN (SELECT id FROM recent)
          AND CASE WHEN t.type = 'X' THEN t.status = 'A' ELSE t.status = 'B' END
    """
    filters = parse_where_clause(sql)
    # Must not crash; returns whatever it could glean
    assert isinstance(filters, ParsedFilters)


def test_empty_sql_returns_empty_filters():
    assert parse_where_clause("") == ParsedFilters(interpretation="", implicit_filters=[])


def test_no_where_clause_returns_empty_filters():
    filters = parse_where_clause("SELECT * FROM transaction")
    assert filters.implicit_filters == []
