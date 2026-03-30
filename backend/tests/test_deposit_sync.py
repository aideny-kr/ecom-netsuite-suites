"""Tests for NetSuite deposit sync — deterministic parts only.

Covers: payout ID regex extraction, date parsing, currency normalization.
No live API calls.
"""

from __future__ import annotations

from app.services.ingestion.netsuite_deposit_sync import (
    _normalize_currency,
    _parse_date,
    extract_payout_id,
)


class TestExtractPayoutId:
    """Payout ID regex extraction from memo field."""

    def test_standard_payout_id(self):
        assert extract_payout_id("Stripe payout po_1abc2def3ghi4jkl5mno") == "po_1abc2def3ghi4jkl5mno"

    def test_payout_id_in_sentence(self):
        memo = "Bank deposit for po_9XyZ8WvU7TsR6QpO5NmL from Stripe"
        assert extract_payout_id(memo) == "po_9XyZ8WvU7TsR6QpO5NmL"

    def test_payout_id_from_url(self):
        memo = "See stripe.com/payouts/po_abc123def456ghi789jkl"
        result = extract_payout_id(memo)
        assert result is not None
        assert "abc123def456ghi789jkl" in result

    def test_payout_keyword(self):
        memo = "Payout: abc123def456ghi789jklmno"
        result = extract_payout_id(memo)
        assert result is not None

    def test_no_match_returns_none(self):
        assert extract_payout_id("Regular bank deposit") is None
        assert extract_payout_id("") is None
        assert extract_payout_id(None) is None

    def test_short_strings_no_false_positive(self):
        assert extract_payout_id("po_abc") is None  # too short

    def test_multiple_ids_returns_first(self):
        memo = "po_firstmatch12345678901234 and po_secondmatch1234567890123"
        result = extract_payout_id(memo)
        assert result == "po_firstmatch12345678901234"


class TestNormalizeCurrency:
    def test_three_letter_code(self):
        assert _normalize_currency("USD") == "USD"
        assert _normalize_currency("EUR") == "EUR"
        assert _normalize_currency("GBP") == "GBP"

    def test_display_names(self):
        assert _normalize_currency("US Dollar") == "USD"
        assert _normalize_currency("Euro") == "EUR"
        assert _normalize_currency("British Pound") == "GBP"
        assert _normalize_currency("Canadian Dollar") == "CAD"

    def test_case_insensitive(self):
        assert _normalize_currency("usd") == "USD"
        assert _normalize_currency("Eur") == "EUR"

    def test_unknown_defaults_to_usd(self):
        assert _normalize_currency("Unknown Currency") == "USD"


class TestParseDate:
    def test_iso_format(self):
        from datetime import date

        assert _parse_date("2026-03-15") == date(2026, 3, 15)

    def test_us_format(self):
        from datetime import date

        assert _parse_date("3/15/2026") == date(2026, 3, 15)

    def test_iso_with_time(self):
        from datetime import date

        assert _parse_date("2026-03-15T10:30:00") == date(2026, 3, 15)

    def test_none_input(self):
        assert _parse_date(None) is None

    def test_invalid_format(self):
        assert _parse_date("not-a-date") is None
