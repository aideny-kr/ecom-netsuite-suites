"""Tests for NetSuite deposit sync — deterministic parts only.

Covers: payout ID regex extraction, date parsing, currency normalization.
No live API calls.

Also covers (DB-backed, R3 Part 1 / Task T3) the per-tenant order_ref_pattern
threading through ``sync_netsuite_deposits``: the network boundary is patched but
the pattern load and the netsuite_postings upsert hit the real docker Postgres.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.models.canonical import NetsuitePosting
from app.models.tenant import TenantConfig
from app.services.ingestion import netsuite_deposit_sync
from app.services.ingestion.netsuite_deposit_sync import (
    _normalize_currency,
    _parse_date,
    extract_order_ref,
    extract_payout_id,
    sync_netsuite_deposits,
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


class TestExtractOrderRef:
    """Order reference extraction from sales order display name."""

    def test_extract_from_sales_order_display(self):
        """Sales Order #R577684612 → R577684612"""
        assert extract_order_ref("Sales Order #R577684612") == "R577684612"

    def test_extract_plain_order_ref(self):
        assert extract_order_ref("R628489275") == "R628489275"

    def test_extract_from_longer_string(self):
        assert extract_order_ref("SO created from R123456789 via import") == "R123456789"

    def test_none_returns_none(self):
        assert extract_order_ref(None) is None

    def test_empty_string_returns_none(self):
        assert extract_order_ref("") is None

    def test_no_match_returns_none(self):
        assert extract_order_ref("Sales Order #12345") is None

    def test_short_r_number_no_match(self):
        """R followed by fewer than 9 digits should not match."""
        assert extract_order_ref("R12345678") is None

    def test_multiple_refs_returns_first(self):
        assert extract_order_ref("R111111111 and R222222222") == "R111111111"


# ---------------------------------------------------------------------------
# DB-backed: per-tenant order_ref_pattern threading through sync_netsuite_deposits
# (R3 Part 1, Task T3). The NetSuite network boundary (connection, token,
# decrypt, SuiteQL) is patched; the pattern load and the netsuite_postings upsert
# run against the local docker Postgres via the conftest ``db`` fixture (rolled
# back per test). Asserts the stored related_payout_id reflects THIS tenant's
# pattern, and that a NULL-pattern tenant (Framework) extracts R\d{9} identically.
# The existing payout-id fallback path stays intact.
#
# Written rigorously following the recon DB-test patterns but NOT run in the
# implementer environment (no DB here); the PM runs them post-flight.
# ---------------------------------------------------------------------------


def _patch_netsuite_boundary(*, suiteql_rows: list[dict]):
    """Patch the NetSuite network boundary of sync_netsuite_deposits.

    Returns a list of patch context managers. The DB (pattern load + posting
    upsert) is intentionally NOT patched so the real Postgres path runs.
    ``suiteql_rows`` are returned as dict rows (the sync handles both list-rows +
    dict-rows; dicts keep the test column-order-independent).
    """
    fake_connection = SimpleNamespace(encrypted_credentials="enc::creds")
    return [
        patch.object(
            netsuite_deposit_sync,
            "get_netsuite_rest_connection",
            new=AsyncMock(return_value=fake_connection),
        ),
        patch.object(
            netsuite_deposit_sync,
            "get_valid_token",
            new=AsyncMock(return_value="fake-token"),
        ),
        patch.object(
            netsuite_deposit_sync,
            "decrypt_credentials",
            new=lambda _enc: {"account_id": "ACME123"},
        ),
        patch.object(
            netsuite_deposit_sync,
            "execute_suiteql_via_rest",
            new=AsyncMock(return_value={"columns": [], "rows": suiteql_rows}),
        ),
    ]


async def _run_sync_and_read_back(db, tenant_id, *, internal_id: str, rows: list[dict]):
    """Run sync_netsuite_deposits with the network patched, return the stored row."""
    patches = _patch_netsuite_boundary(suiteql_rows=rows)
    for p in patches:
        p.start()
    try:
        result = await sync_netsuite_deposits(
            db=db,
            tenant_id=str(tenant_id),
            date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 31),
        )
    finally:
        for p in patches:
            p.stop()

    assert not result.errors, result.errors
    posting = (
        await db.execute(
            select(NetsuitePosting).where(
                NetsuitePosting.tenant_id == tenant_id,
                NetsuitePosting.netsuite_internal_id == internal_id,
            )
        )
    ).scalar_one()
    return result, posting


class TestSyncDepositsUsesTenantPattern:
    """sync_netsuite_deposits threads THIS tenant's order_ref_pattern through extraction."""

    async def test_custom_pattern_tenant_stores_custom_ref(self, db, tenant_a):
        """A custom-pattern tenant stores related_payout_id via that pattern."""
        cfg = (await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_a.id))).scalar_one()
        cfg.order_ref_pattern = r"(#\d{4,})"
        await db.flush()

        rows = [
            {
                "internal_id": "900001",
                "document_number": "DEP-1",
                "transaction_date": "2026-03-16",
                "record_type": "Deposit",
                "memo": "bank deposit",
                "amount": "100.00",
                "currency_name": "USD",
                "account_id": "10",
                "account_name": "Bank",
                "subsidiary_id": "1",
                # No R\d{9}; only a #-prefixed number the custom pattern captures.
                "sales_order_ref": "Sales Order #100423",
            }
        ]
        _result, posting = await _run_sync_and_read_back(db, tenant_a.id, internal_id="900001", rows=rows)
        assert posting.related_payout_id == "#100423"

    async def test_null_pattern_tenant_stores_r9_identically(self, db, tenant_a):
        """A NULL-pattern tenant (Framework) stores the R\\d{9} ref byte-identically."""
        cfg = (await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_a.id))).scalar_one()
        assert cfg.order_ref_pattern is None  # conftest default

        rows = [
            {
                "internal_id": "900002",
                "document_number": "DEP-2",
                "transaction_date": "2026-03-16",
                "record_type": "CustDep",
                "memo": "customer deposit",
                "amount": "250.00",
                "currency_name": "USD",
                "account_id": "10",
                "account_name": "Bank",
                "subsidiary_id": "1",
                "sales_order_ref": "Sales Order #R577684612",
            }
        ]
        _result, posting = await _run_sync_and_read_back(db, tenant_a.id, internal_id="900002", rows=rows)
        assert posting.related_payout_id == "R577684612"

    async def test_payout_id_fallback_intact_when_no_order_ref(self, db, tenant_a):
        """With no sales_order_ref, the legacy payout-id memo fallback still wins."""
        # NULL pattern; sales_order_ref absent so extract_order_ref returns None and
        # the memo payout-id fallback must populate related_payout_id.
        rows = [
            {
                "internal_id": "900003",
                "document_number": "DEP-3",
                "transaction_date": "2026-03-16",
                "record_type": "Deposit",
                "memo": "Stripe payout po_1abc2def3ghi4jkl5mno",
                "amount": "75.00",
                "currency_name": "USD",
                "account_id": "10",
                "account_name": "Bank",
                "subsidiary_id": "1",
                "sales_order_ref": "",
            }
        ]
        _result, posting = await _run_sync_and_read_back(db, tenant_a.id, internal_id="900003", rows=rows)
        assert posting.related_payout_id == "po_1abc2def3ghi4jkl5mno"
