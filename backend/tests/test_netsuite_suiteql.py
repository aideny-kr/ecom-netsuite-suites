"""Tests for real SuiteQL tool: allowlist, LIMIT enforcement, read-only checks."""

import pytest

from app.mcp.tools.netsuite_suiteql import enforce_limit, parse_tables, validate_query

# ---------------------------------------------------------------------------
# parse_tables
# ---------------------------------------------------------------------------


class TestParseTables:
    def test_simple_from(self):
        assert parse_tables("SELECT id FROM transaction") == {"transaction"}

    def test_join(self):
        tables = parse_tables("SELECT t.id, c.name FROM transaction t JOIN customer c ON t.entity = c.id")
        assert tables == {"transaction", "customer"}

    def test_multiple_joins(self):
        query = (
            "SELECT t.id FROM transaction t JOIN customer c ON t.entity = c.id JOIN subsidiary s ON t.subsidiary = s.id"
        )
        tables = parse_tables(query)
        assert tables == {"transaction", "customer", "subsidiary"}

    def test_case_insensitive(self):
        assert parse_tables("select id from Transaction") == {"transaction"}

    def test_no_tables(self):
        assert parse_tables("SELECT 1 AS health") == set()

    def test_subquery(self):
        tables = parse_tables("SELECT id FROM transaction WHERE entity IN (SELECT id FROM customer)")
        assert tables == {"transaction", "customer"}


# ---------------------------------------------------------------------------
# validate_query — allowlist enforcement
# ---------------------------------------------------------------------------

ALLOWED = {"transaction", "customer", "item", "account"}


class TestValidateQuery:
    def test_allowed_table(self):
        validate_query("SELECT id FROM transaction", ALLOWED)

    def test_disallowed_table(self):
        with pytest.raises(ValueError, match="disallowed tables.*secret_table"):
            validate_query("SELECT id FROM secret_table", ALLOWED)

    def test_mixed_allowed_and_disallowed(self):
        with pytest.raises(ValueError, match="disallowed"):
            validate_query(
                "SELECT t.id FROM transaction t JOIN secret_table s ON t.id = s.id",
                ALLOWED,
            )

    def test_insert_rejected(self):
        with pytest.raises(ValueError, match="read-only"):
            validate_query("INSERT INTO transaction (id) VALUES (1)", ALLOWED)

    def test_update_rejected(self):
        with pytest.raises(ValueError, match="read-only"):
            validate_query("UPDATE transaction SET status = 'closed'", ALLOWED)

    def test_delete_rejected(self):
        with pytest.raises(ValueError, match="read-only"):
            validate_query("DELETE FROM transaction WHERE id = 1", ALLOWED)

    def test_drop_rejected(self):
        with pytest.raises(ValueError, match="read-only"):
            validate_query("DROP TABLE transaction", ALLOWED)

    def test_select_allowed(self):
        validate_query("SELECT id, companyname FROM customer", ALLOWED)

    def test_empty_query_rejected(self):
        with pytest.raises(ValueError, match="read-only"):
            validate_query("", ALLOWED)


# ---------------------------------------------------------------------------
# enforce_limit
# ---------------------------------------------------------------------------


class TestEnforceLimit:
    def test_no_limit_injects_fetch(self):
        result = enforce_limit("SELECT id FROM transaction", 1000)
        assert result == "SELECT id FROM transaction FETCH FIRST 1000 ROWS ONLY"

    def test_existing_fetch_under_max_unchanged(self):
        query = "SELECT id FROM transaction FETCH FIRST 50 ROWS ONLY"
        result = enforce_limit(query, 1000)
        assert "FETCH FIRST 50 ROWS ONLY" in result

    def test_existing_fetch_over_max_capped(self):
        query = "SELECT id FROM transaction FETCH FIRST 5000 ROWS ONLY"
        result = enforce_limit(query, 1000)
        assert "FETCH FIRST 1000 ROWS ONLY" in result
        assert "5000" not in result

    def test_existing_limit_under_max_unchanged(self):
        query = "SELECT id FROM transaction LIMIT 50"
        result = enforce_limit(query, 1000)
        assert "LIMIT 50" in result

    def test_existing_limit_over_max_capped(self):
        query = "SELECT id FROM transaction LIMIT 5000"
        result = enforce_limit(query, 1000)
        assert "LIMIT 1000" in result
        assert "5000" not in result

    def test_strips_trailing_semicolon(self):
        result = enforce_limit("SELECT id FROM transaction;", 100)
        assert result.endswith("FETCH FIRST 100 ROWS ONLY")
        assert ";" not in result

    def test_case_insensitive_fetch(self):
        query = "SELECT id FROM transaction fetch first 50 rows only"
        result = enforce_limit(query, 1000)
        assert "FETCH FIRST 50 ROWS ONLY" in result


# ---------------------------------------------------------------------------
# Malformed query → graceful handling
# ---------------------------------------------------------------------------


class TestMalformedQueries:
    def test_non_select_no_tables(self):
        """A query that's not SELECT should be rejected by validate_query."""
        with pytest.raises(ValueError, match="read-only"):
            validate_query("EXPLAIN SELECT 1", ALLOWED)

    def test_semicolon_injection_blocked(self):
        """Multi-statement with forbidden keyword is caught."""
        with pytest.raises(ValueError, match="read-only"):
            validate_query("SELECT id FROM transaction; DROP TABLE transaction", ALLOWED)
