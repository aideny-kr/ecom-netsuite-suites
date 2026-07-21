"""Tests for NetSuite deposit sync — deterministic parts only.

Covers: payout ID regex extraction, date parsing, currency normalization.
No live API calls.

Also covers (DB-backed, R3 Part 1 / Task T3) the per-tenant order_ref_pattern
threading through ``sync_netsuite_deposits``: the network boundary is patched but
the pattern load and the netsuite_postings upsert hit the real docker Postgres.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.core.encryption import encrypt_credentials
from app.models.canonical import NetsuitePosting
from app.models.connection import Connection
from app.models.pipeline import CursorState
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

    def test_taiwan_maps_to_twd(self):
        """Live NetSuite verification: Taiwan subsidiaries' currency display name
        is literally "Taiwan", not "Taiwan Dollar" or "New Taiwan Dollar"."""
        assert _normalize_currency("Taiwan") == "TWD"
        assert _normalize_currency("taiwan") == "TWD"
        assert _normalize_currency("TWD") == "TWD"

    def test_unknown_label_does_not_default_to_usd(self):
        """An unrecognized label must never silently become "USD" — that
        recreates the exact guess-USD lie Phase A exists to kill. It passes
        through as its normalized (stripped/uppercased) form instead, truncated
        to fit the `currency`/`transaction_currency` VARCHAR(3) columns."""
        result = _normalize_currency("Wakanda Dollar")
        assert result != "USD"
        assert result == "WAK"

    def test_unknown_label_logs_warning(self):
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            result = _normalize_currency("Wakanda Dollar")

        assert result == "WAK"
        warnings = [
            e
            for e in logs
            if e.get("log_level") == "warning" and e.get("event") == "netsuite_deposit_sync.unmapped_currency_label"
        ]
        assert warnings, f"expected an unmapped_currency_label warning, got: {logs}"
        assert warnings[0]["label"] == "WAKANDA DOLLAR"

    def test_existing_mappings_unchanged(self):
        assert _normalize_currency("Swiss Franc") == "CHF"
        assert _normalize_currency("Singapore Dollar") == "SGD"
        assert _normalize_currency("New Zealand Dollar") == "NZD"
        assert _normalize_currency("Japanese Yen") == "JPY"
        assert _normalize_currency("Australian Dollar") == "AUD"


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


async def _seed_netsuite_connection(db, tenant_id) -> Connection:
    """Insert a real active NetSuite REST connection (satisfies the cursor FK)."""
    connection = Connection(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider="netsuite",
        label="Test NetSuite",
        status="active",
        auth_type="oauth2",
        encrypted_credentials=encrypt_credentials({"account_id": "ACME123"}),
    )
    db.add(connection)
    await db.flush()
    return connection


def _patch_netsuite_boundary(*, connection: Connection, suiteql_rows: list[dict]):
    """Patch the NetSuite network boundary of sync_netsuite_deposits.

    Returns a list of patch context managers. The DB (pattern load, posting
    upsert, freshness-cursor upsert) is intentionally NOT patched so the real
    Postgres path runs. A REAL ``connection`` is returned by the patched
    ``get_netsuite_rest_connection`` so ``connection.id`` satisfies the
    cursor_states FK. ``suiteql_rows`` are returned as dict rows (the sync handles
    both list-rows + dict-rows; dicts keep the test column-order-independent).
    """
    return [
        patch.object(
            netsuite_deposit_sync,
            "get_netsuite_rest_connection",
            new=AsyncMock(return_value=connection),
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
    connection = await _seed_netsuite_connection(db, tenant_id)
    patches = _patch_netsuite_boundary(connection=connection, suiteql_rows=rows)
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


# ---------------------------------------------------------------------------
# DB-backed: freshness cursor maintained by sync_netsuite_deposits (cursor bug)
#
# The recon data-status banner reads CursorState.last_synced_at WHERE
# object_type='netsuite_deposits' to show "NetSuite: N deposits · Xd ago". The
# nightly Celery task calls sync_netsuite_deposits directly (NOT the manual
# trigger endpoint), so the service ITSELF must bump the cursor on every
# successful run — otherwise the banner freezes at the last manual sync even
# though deposits land daily.
#
# Here we seed a REAL Connection row (the cursor_states.connection_id FK requires
# it) and patch get_netsuite_rest_connection to return it, so connection.id is
# FK-valid and the cursor upsert hits the real docker Postgres. The token /
# decrypt / SuiteQL boundary is patched. Asserts a CursorState row for
# (connection.id, 'netsuite_deposits') exists with a just-now last_synced_at and
# cursor_value == date_to.isoformat().
# ---------------------------------------------------------------------------


class TestSyncDepositsCurrencyTruth:
    """Phase A / P0 fix: the sync previously selected ``t.total`` (subsidiary BASE
    amount) but labeled it with ``BUILTIN.DF(t.currency)`` (TRANSACTION currency) —
    a CAD/CHF/SGD/NZD-labeled deposit actually carried a USD amount. The fix adds a
    subsidiary join for the honest base currency and separately preserves the
    transaction currency, foreign amount, and exchange rate instead of discarding them.
    """

    async def test_foreign_currency_deposit_stores_honest_currency_fields(self, db, tenant_a):
        """A CHF-labeled deposit whose amount is really USD (subsidiary base) must
        end up with currency=USD (base, via the subsidiary join), transaction_currency=CHF
        (the label NetSuite put on the transaction), and the foreign amount/rate preserved.
        """
        rows = [
            {
                "internal_id": "920001",
                "document_number": "DEP-FX-1",
                "transaction_date": "2026-03-16",
                "record_type": "Deposit",
                "memo": "bank deposit",
                "amount": "13500.00",  # t.total — subsidiary BASE currency amount
                "currency_name": "Swiss Franc",  # BUILTIN.DF(t.currency) — TRANSACTION currency
                "base_currency_name": "US Dollar",  # BUILTIN.DF(subsidiary.currency) — the fix
                "foreign_amount": "10000.00",  # t.foreigntotal — TRANSACTION currency amount
                "exchange_rate": "1.35",  # t.exchangerate
                "account_id": "10",
                "account_name": "Bank",
                "subsidiary_id": "1",
                "sales_order_ref": "",
            }
        ]
        _result, posting = await _run_sync_and_read_back(db, tenant_a.id, internal_id="920001", rows=rows)

        assert posting.amount == Decimal("13500.00")  # unchanged meaning — base amount
        assert posting.currency == "USD"  # FIXED: base currency, not the transaction label
        assert posting.transaction_currency == "CHF"
        assert posting.foreign_amount == Decimal("10000.00")
        assert posting.exchange_rate == Decimal("1.35")

    async def test_domestic_deposit_currency_and_transaction_currency_match(self, db, tenant_a):
        """A plain USD deposit: base and transaction currency are the same; foreign_amount
        and exchange_rate are still populated from the (also-USD) transaction-currency fields.
        """
        rows = [
            {
                "internal_id": "920002",
                "document_number": "DEP-FX-2",
                "transaction_date": "2026-03-16",
                "record_type": "Deposit",
                "memo": "bank deposit",
                "amount": "500.00",
                "currency_name": "US Dollar",
                "base_currency_name": "US Dollar",
                "foreign_amount": "500.00",
                "exchange_rate": "1.00",
                "account_id": "10",
                "account_name": "Bank",
                "subsidiary_id": "1",
                "sales_order_ref": "",
            }
        ]
        _result, posting = await _run_sync_and_read_back(db, tenant_a.id, internal_id="920002", rows=rows)

        assert posting.currency == "USD"
        assert posting.transaction_currency == "USD"
        assert posting.foreign_amount == Decimal("500.00")
        assert posting.exchange_rate == Decimal("1.00")

    async def test_subsidiary_join_unavailable_falls_back_without_hardcoding_usd(self, db, tenant_a):
        """If the subsidiary join yields nothing for this record shape (no
        ``base_currency_name`` key/value in the row), ``currency`` MUST fall back to
        today's behavior (the transaction-currency label) — never a hardcoded "USD" guess.
        transaction_currency/foreign_amount/exchange_rate are still populated when present.
        """
        rows = [
            {
                "internal_id": "920003",
                "document_number": "DEP-FX-3",
                "transaction_date": "2026-03-16",
                "record_type": "Deposit",
                "memo": "bank deposit",
                "amount": "9000.00",
                "currency_name": "Canadian Dollar",
                # no base_currency_name key at all — simulates join returning NULL/absent
                "foreign_amount": "12000.00",
                "exchange_rate": "0.75",
                "account_id": "10",
                "account_name": "Bank",
                "subsidiary_id": "1",
                "sales_order_ref": "",
            }
        ]
        _result, posting = await _run_sync_and_read_back(db, tenant_a.id, internal_id="920003", rows=rows)

        # documents the fallback: same as pre-fix behavior, not a guess
        assert posting.currency == "CAD"
        assert posting.transaction_currency == "CAD"
        assert posting.foreign_amount == Decimal("12000.00")
        assert posting.exchange_rate == Decimal("0.75")

    async def test_missing_foreign_fields_are_stored_as_none(self, db, tenant_a):
        """Rows with no foreign_amount/exchange_rate (e.g. non-multi-currency subsidiary)
        must store NULL, not zero or a fabricated value.
        """
        rows = [
            {
                "internal_id": "920004",
                "document_number": "DEP-FX-4",
                "transaction_date": "2026-03-16",
                "record_type": "Deposit",
                "memo": "bank deposit",
                "amount": "100.00",
                "currency_name": "US Dollar",
                "base_currency_name": "US Dollar",
                "account_id": "10",
                "account_name": "Bank",
                "subsidiary_id": "1",
                "sales_order_ref": "",
            }
        ]
        _result, posting = await _run_sync_and_read_back(db, tenant_a.id, internal_id="920004", rows=rows)

        assert posting.foreign_amount is None
        assert posting.exchange_rate is None

    async def test_upsert_updates_currency_truth_columns_on_resync(self, db, tenant_a):
        """A re-sync of the same NetSuite internal_id (rate/booking correction) must
        update transaction_currency/foreign_amount/exchange_rate, not just insert-once.
        """
        connection = await _seed_netsuite_connection(db, tenant_a.id)
        first_rows = [
            {
                "internal_id": "920005",
                "document_number": "DEP-FX-5",
                "transaction_date": "2026-03-16",
                "record_type": "Deposit",
                "memo": "bank deposit",
                "amount": "13500.00",
                "currency_name": "Swiss Franc",
                "base_currency_name": "US Dollar",
                "foreign_amount": "10000.00",
                "exchange_rate": "1.35",
                "account_id": "10",
                "account_name": "Bank",
                "subsidiary_id": "1",
                "sales_order_ref": "",
            }
        ]
        second_rows = [
            {
                **first_rows[0],
                "amount": "13600.00",
                "foreign_amount": "10000.00",
                "exchange_rate": "1.36",
            }
        ]

        patches = _patch_netsuite_boundary(connection=connection, suiteql_rows=first_rows)
        for p in patches:
            p.start()
        try:
            await sync_netsuite_deposits(
                db=db, tenant_id=str(tenant_a.id), date_from=date(2026, 3, 1), date_to=date(2026, 3, 31)
            )
        finally:
            for p in patches:
                p.stop()

        patches = _patch_netsuite_boundary(connection=connection, suiteql_rows=second_rows)
        for p in patches:
            p.start()
        try:
            await sync_netsuite_deposits(
                db=db, tenant_id=str(tenant_a.id), date_from=date(2026, 3, 1), date_to=date(2026, 3, 31)
            )
        finally:
            for p in patches:
                p.stop()

        posting = (
            await db.execute(
                select(NetsuitePosting).where(
                    NetsuitePosting.tenant_id == tenant_a.id,
                    NetsuitePosting.netsuite_internal_id == "920005",
                )
            )
        ).scalar_one()
        assert posting.amount == Decimal("13600.00")
        assert posting.exchange_rate == Decimal("1.36")


class TestSyncDepositsMaintainsFreshnessCursor:
    """sync_netsuite_deposits writes the netsuite_deposits freshness cursor on success."""

    async def test_cursor_written_on_successful_sync(self, db, tenant_a):
        connection = await _seed_netsuite_connection(db, tenant_a.id)

        rows = [
            {
                "internal_id": "910001",
                "document_number": "DEP-CUR-1",
                "transaction_date": "2026-03-16",
                "record_type": "Deposit",
                "memo": "bank deposit",
                "amount": "100.00",
                "currency_name": "USD",
                "account_id": "10",
                "account_name": "Bank",
                "subsidiary_id": "1",
                "sales_order_ref": "",
            }
        ]

        date_from = date(2026, 3, 1)
        date_to = date(2026, 3, 31)

        patches = _patch_netsuite_boundary(connection=connection, suiteql_rows=rows)
        for p in patches:
            p.start()
        try:
            before = datetime.now(timezone.utc)
            result = await sync_netsuite_deposits(
                db=db,
                tenant_id=str(tenant_a.id),
                date_from=date_from,
                date_to=date_to,
            )
            after = datetime.now(timezone.utc)
        finally:
            for p in patches:
                p.stop()

        assert not result.errors, result.errors

        cursor = (
            await db.execute(
                select(CursorState).where(
                    CursorState.connection_id == connection.id,
                    CursorState.object_type == "netsuite_deposits",
                )
            )
        ).scalar_one_or_none()

        assert cursor is not None, "sync_netsuite_deposits did not write a netsuite_deposits cursor"
        assert cursor.cursor_value == date_to.isoformat()
        assert cursor.last_synced_at is not None
        # last_synced_at should be ~now (within the test window, with a little slack)
        synced = cursor.last_synced_at
        if synced.tzinfo is None:
            synced = synced.replace(tzinfo=timezone.utc)
        assert (before - timedelta(seconds=5)) <= synced <= (after + timedelta(seconds=5))

    async def test_cursor_write_failure_does_not_fail_sync(self, db, tenant_a):
        """A cursor-write failure must NOT fail an otherwise-successful deposit sync.

        The cursor is best-effort freshness metadata: a transient failure writing it
        must neither propagate an exception (which would 500 the manual endpoint /
        crash the nightly task) nor lose the already-committed deposits.
        """
        connection = await _seed_netsuite_connection(db, tenant_a.id)

        rows = [
            {
                "internal_id": "910002",
                "document_number": "DEP-CUR-2",
                "transaction_date": "2026-03-16",
                "record_type": "Deposit",
                "memo": "bank deposit",
                "amount": "100.00",
                "currency_name": "USD",
                "account_id": "10",
                "account_name": "Bank",
                "subsidiary_id": "1",
                "sales_order_ref": "",
            }
        ]

        date_from = date(2026, 3, 1)
        date_to = date(2026, 3, 31)

        patches = _patch_netsuite_boundary(connection=connection, suiteql_rows=rows)
        # Patch save_cursor_async (as imported/used by the service) to blow up.
        patches.append(
            patch.object(
                netsuite_deposit_sync,
                "save_cursor_async",
                new=AsyncMock(side_effect=RuntimeError("simulated cursor write failure")),
            )
        )
        for p in patches:
            p.start()
        try:
            # (a) NO exception must propagate.
            result = await sync_netsuite_deposits(
                db=db,
                tenant_id=str(tenant_a.id),
                date_from=date_from,
                date_to=date_to,
            )
        finally:
            for p in patches:
                p.stop()

        # (b) The result still reflects synced deposits.
        assert not result.errors, result.errors
        assert result.records_synced > 0

        # (c) The deposits were persisted despite the cursor failure.
        posting = (
            await db.execute(
                select(NetsuitePosting).where(
                    NetsuitePosting.tenant_id == tenant_a.id,
                    NetsuitePosting.netsuite_internal_id == "910002",
                )
            )
        ).scalar_one()
        assert posting is not None
