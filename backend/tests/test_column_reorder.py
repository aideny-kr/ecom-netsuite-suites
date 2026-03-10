"""Tests for SELECT-clause column reordering in SuiteQL results."""

from app.mcp.tools.netsuite_suiteql import parse_select_aliases, reorder_columns


class TestParseSelectAliases:
    """Unit tests for extracting column aliases from SELECT clauses."""

    def test_simple_dotted_columns(self):
        query = "SELECT t.tranid, t.status, tl.quantity FROM transaction t"
        assert parse_select_aliases(query) == ["tranid", "status", "quantity"]

    def test_explicit_as_aliases(self):
        query = "SELECT t.tranid AS po_number, tl.quantity AS ordered_qty FROM transaction t"
        assert parse_select_aliases(query) == ["po_number", "ordered_qty"]

    def test_builtin_df_with_alias(self):
        query = "SELECT t.tranid, BUILTIN.DF(t.status) AS status, BUILTIN.DF(t.entity) AS vendor FROM transaction t"
        assert parse_select_aliases(query) == ["tranid", "status", "vendor"]

    def test_aggregate_with_alias(self):
        query = "SELECT t.entity, SUM(tl.quantity) AS total_qty, COUNT(*) AS row_count FROM transaction t"
        assert parse_select_aliases(query) == ["entity", "total_qty", "row_count"]

    def test_nested_functions(self):
        query = (
            "SELECT BUILTIN.CONSOLIDATE(tal.amount, 'INCOME', 'DEFAULT', 'DEFAULT', 1, ap.id, 'DEFAULT')"
            " AS amount FROM transactionaccountingline tal"
        )
        aliases = parse_select_aliases(query)
        assert aliases == ["amount"]

    def test_mixed_expressions(self):
        query = (
            "SELECT t.tranid, BUILTIN.DF(t.status) AS status, "
            "tl.quantity AS ordered_qty, tl.quantityreceived "
            "FROM transaction t"
        )
        assert parse_select_aliases(query) == ["tranid", "status", "ordered_qty", "quantityreceived"]

    def test_case_insensitive(self):
        query = "select t.TranId, t.Status from transaction t"
        assert parse_select_aliases(query) == ["tranid", "status"]

    def test_no_select(self):
        assert parse_select_aliases("not a query") == []

    def test_star_select(self):
        query = "SELECT * FROM transaction"
        aliases = parse_select_aliases(query)
        assert aliases == ["*"]


class TestReorderColumns:
    """Unit tests for reordering API columns to match SELECT order."""

    def test_basic_reorder(self):
        api_cols = ["quantity", "status", "tranid"]  # alphabetical from API
        query = "SELECT t.tranid, t.status, tl.quantity FROM transaction t"
        assert reorder_columns(api_cols, query) == ["tranid", "status", "quantity"]

    def test_with_aliases(self):
        api_cols = ["ordered_qty", "po_number", "status"]
        query = (
            "SELECT t.tranid AS po_number, BUILTIN.DF(t.status) AS status,"
            " tl.quantity AS ordered_qty FROM transaction t"
        )
        assert reorder_columns(api_cols, query) == ["po_number", "status", "ordered_qty"]

    def test_extra_api_columns_appended(self):
        """Columns from API not in SELECT (e.g. links leftover) go at the end."""
        api_cols = ["extra_col", "status", "tranid"]
        query = "SELECT t.tranid, t.status FROM transaction t"
        assert reorder_columns(api_cols, query) == ["tranid", "status", "extra_col"]

    def test_missing_select_columns_skipped(self):
        """If SELECT has a column not in API results, skip it gracefully."""
        api_cols = ["status", "tranid"]
        query = "SELECT t.tranid, t.status, t.memo FROM transaction t"
        result = reorder_columns(api_cols, query)
        assert result == ["tranid", "status"]

    def test_no_select_returns_original(self):
        api_cols = ["b", "a", "c"]
        assert reorder_columns(api_cols, "not a query") == ["b", "a", "c"]

    def test_empty_api_columns(self):
        query = "SELECT t.tranid FROM transaction t"
        assert reorder_columns([], query) == []

    def test_real_world_po_query(self):
        """Simulate the actual PO query that was showing alphabetical columns."""
        api_cols = [
            "expectedreceiptdate",
            "itemid",
            "location",
            "ordered_qty",
            "pending_qty",
            "po_duedate",
            "po_number",
            "received_qty",
            "status",
        ]
        query = (
            "SELECT t.tranid AS po_number, BUILTIN.DF(t.entity) AS vendor, "
            "i.itemid, tl.quantity AS ordered_qty, tl.quantityreceived AS received_qty, "
            "tl.quantitybilled AS pending_qty, t.duedate AS po_duedate, "
            "tl.expectedreceiptdate, BUILTIN.DF(t.status) AS status, "
            "BUILTIN.DF(tl.location) AS location "
            "FROM transaction t"
        )
        result = reorder_columns(api_cols, query)
        # po_number should be first, then vendor (missing from API), then itemid, etc.
        assert result[0] == "po_number"
        assert result[1] == "itemid"  # vendor not in api_cols, so skipped
        assert result[2] == "ordered_qty"
        assert result[3] == "received_qty"
        assert result[-1] == "location"
