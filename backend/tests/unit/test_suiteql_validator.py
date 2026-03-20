"""TDD: SuiteQL pre-execution validation.

Catches known syntax errors before they hit NetSuite — saves 1-3 wasted
tool calls per bad query. Fail-open: uncertain queries pass through.
"""

import pytest

from app.services.suiteql_validator import validate_suiteql_query


class TestLimitDetection:
    def test_limit_detected(self):
        result = validate_suiteql_query("SELECT * FROM transaction LIMIT 10")
        assert not result.is_valid
        assert any("LIMIT" in e for e in result.errors)
        assert result.suggested_fix and "FETCH FIRST 10 ROWS ONLY" in result.suggested_fix

    def test_fetch_first_is_fine(self):
        result = validate_suiteql_query("SELECT * FROM transaction FETCH FIRST 100 ROWS ONLY")
        assert result.is_valid


class TestCompoundStatusCodes:
    def test_compound_code_detected(self):
        result = validate_suiteql_query("SELECT * FROM transaction WHERE status = 'SalesOrd:B'")
        assert not result.is_valid
        assert any("Compound" in e for e in result.errors)

    def test_single_letter_codes_fine(self):
        result = validate_suiteql_query("SELECT * FROM transaction WHERE status IN ('B', 'D', 'E')")
        assert result.is_valid


class TestRownumOrderBy:
    def test_rownum_with_order_by(self):
        result = validate_suiteql_query("SELECT * FROM transaction WHERE ROWNUM <= 10 ORDER BY trandate")
        assert not result.is_valid
        assert any("ROWNUM" in e for e in result.errors)


class TestOffset:
    def test_offset_detected(self):
        result = validate_suiteql_query("SELECT * FROM transaction OFFSET 10")
        assert not result.is_valid
        assert any("OFFSET" in e for e in result.errors)


class TestIlike:
    def test_ilike_detected(self):
        result = validate_suiteql_query("SELECT * FROM customer WHERE companyname ILIKE '%frame%'")
        assert not result.is_valid
        assert any("ILIKE" in e for e in result.errors)


class TestBooleanLiterals:
    def test_bare_boolean_detected(self):
        result = validate_suiteql_query("SELECT * FROM item WHERE isinactive = true")
        assert not result.is_valid
        assert any("T" in e and "F" in e for e in result.errors)

    def test_quoted_boolean_fine(self):
        result = validate_suiteql_query("SELECT * FROM item WHERE isinactive = 'true'")
        # Quoted 'true' is a string literal — might be wrong but not a syntax error
        assert result.is_valid


class TestDoubleQuotes:
    def test_double_quotes_for_values(self):
        result = validate_suiteql_query('SELECT * FROM transaction WHERE memo = "test"')
        assert not result.is_valid
        assert any("single quotes" in e for e in result.errors)


class TestMissingFrom:
    def test_missing_from(self):
        result = validate_suiteql_query("SELECT 1 + 1")
        assert not result.is_valid
        assert any("FROM" in e for e in result.errors)


class TestValidQueries:
    def test_simple_valid_query(self):
        result = validate_suiteql_query(
            "SELECT id, tranid FROM transaction WHERE type = 'SalesOrd' AND status = 'B' FETCH FIRST 100 ROWS ONLY"
        )
        assert result.is_valid
        assert result.errors == []

    def test_complex_valid_query(self):
        result = validate_suiteql_query(
            "SELECT t.id, tl.item, BUILTIN.DF(tl.item) AS item_name "
            "FROM transaction t INNER JOIN transactionline tl ON t.id = tl.transaction "
            "WHERE t.type = 'SalesOrd' GROUP BY t.id, tl.item FETCH FIRST 50 ROWS ONLY"
        )
        assert result.is_valid


class TestMultipleErrors:
    def test_multiple_errors_caught(self):
        result = validate_suiteql_query("SELECT * FROM transaction WHERE status = 'SalesOrd:B' LIMIT 10")
        assert not result.is_valid
        assert len(result.errors) >= 2
