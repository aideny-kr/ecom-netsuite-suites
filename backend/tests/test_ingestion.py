"""Tests for Stripe and Shopify ingestion services with mocked APIs."""

import uuid
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import Select

from app.services.ingestion.base import load_cursor, save_cursor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_connection(provider="stripe"):
    """Create a fake Connection object."""
    conn = MagicMock()
    conn.id = uuid.uuid4()
    conn.tenant_id = uuid.uuid4()
    conn.provider = provider
    conn.encrypted_credentials = "encrypted_blob"
    return conn


def _make_stripe_payout(payout_id="po_123", amount=10000, currency="usd", status="paid"):
    """Create a mock Stripe Payout object."""
    p = MagicMock()
    p.id = payout_id
    p.amount = amount
    p.currency = currency
    p.status = status
    p.arrival_date = 1700000000
    p.created = 1700000000
    p.__iter__ = lambda self: iter({"id": payout_id, "amount": amount}.items())
    p.items = lambda: {"id": payout_id, "amount": amount}.items()
    p.keys = lambda: {"id": payout_id, "amount": amount}.keys()
    # Make dict(p) work
    type(p).__iter__ = lambda self: iter({"id": payout_id, "amount": amount})
    return p


def _make_stripe_balance_txn(txn_id="txn_456", amount=5000, fee=150, net=4850):
    t = MagicMock()
    t.id = txn_id
    t.type = "charge"
    t.amount = amount
    t.fee = fee
    t.net = net
    t.currency = "usd"
    t.description = "Test charge"
    t.source = "ch_789"
    return t


def _make_payout_row(source_id="po_123", status="in_transit", arrival_date=None):
    """Create a mock canonical Payout ORM row (as returned by the status-refresh query)."""
    row = MagicMock()
    row.source_id = source_id
    row.status = status
    row.arrival_date = arrival_date
    return row


def _make_stripe_dispute(dispute_id="dp_001", amount=2000, currency="usd"):
    d = MagicMock()
    d.id = dispute_id
    d.amount = amount
    d.currency = currency
    d.status = "needs_response"
    d.reason = "fraudulent"
    d.charge = "ch_789"
    d.created = 1700000001
    return d


class _FakeListResult:
    """Simulate stripe.X.list() with auto_paging_iter."""

    def __init__(self, items):
        self._items = items

    def auto_paging_iter(self):
        return iter(self._items)


# ---------------------------------------------------------------------------
# Stripe sync tests
# ---------------------------------------------------------------------------


class TestStripeSync:
    @patch("app.services.ingestion.stripe_sync.decrypt_credentials")
    @patch("app.services.ingestion.stripe_sync.stripe")
    def test_payout_sync_basic(self, mock_stripe, mock_decrypt):
        """Syncing one payout should call upsert_canonical and save cursor."""
        mock_decrypt.return_value = {"api_key": "sk_test_123"}

        payout = _make_stripe_payout()
        mock_stripe.Payout.list.return_value = _FakeListResult([payout])
        mock_stripe.BalanceTransaction.list.return_value = _FakeListResult([])
        mock_stripe.Dispute.list.return_value = _FakeListResult([])

        db = MagicMock()
        conn = _make_connection("stripe")

        # Make db.execute().scalar_one() return the connection
        db.execute.return_value.scalar_one.return_value = conn
        db.execute.return_value.scalar_one_or_none.return_value = None

        with (
            patch("app.services.ingestion.stripe_sync.load_cursor", return_value=None),
            patch("app.services.ingestion.stripe_sync.save_cursor"),
            patch("app.services.ingestion.stripe_sync.upsert_canonical") as mock_upsert,
        ):
            from app.services.ingestion.stripe_sync import sync_stripe

            result = sync_stripe(db, str(conn.id), str(conn.tenant_id))

        assert result["payouts_synced"] == 1
        assert result["payout_lines_synced"] == 0
        assert result["disputes_synced"] == 0
        mock_upsert.assert_called()
        # First call should be the payout upsert
        call_args = mock_upsert.call_args_list[0]
        assert call_args.kwargs.get("dedupe_key") or "stripe:po_123" in str(call_args)

    @patch("app.services.ingestion.stripe_sync.decrypt_credentials")
    @patch("app.services.ingestion.stripe_sync.stripe")
    def test_dispute_sync(self, mock_stripe, mock_decrypt):
        mock_decrypt.return_value = {"api_key": "sk_test_123"}

        dispute = _make_stripe_dispute()
        mock_stripe.Payout.list.return_value = _FakeListResult([])
        mock_stripe.BalanceTransaction.list.return_value = _FakeListResult([])
        mock_stripe.Dispute.list.return_value = _FakeListResult([dispute])

        db = MagicMock()
        conn = _make_connection("stripe")
        db.execute.return_value.scalar_one.return_value = conn

        with (
            patch("app.services.ingestion.stripe_sync.load_cursor", return_value=None),
            patch("app.services.ingestion.stripe_sync.save_cursor") as mock_save,
            patch("app.services.ingestion.stripe_sync.upsert_canonical"),
        ):
            from app.services.ingestion.stripe_sync import sync_stripe

            result = sync_stripe(db, str(conn.id), str(conn.tenant_id))

        assert result["disputes_synced"] == 1
        mock_save.assert_called()

    @patch("app.services.ingestion.stripe_sync.decrypt_credentials")
    @patch("app.services.ingestion.stripe_sync.stripe")
    def test_dispute_cursor_saves_max_timestamp(self, mock_stripe, mock_decrypt):
        """Cursor should save the NEWEST dispute timestamp, not the last iterated."""
        mock_decrypt.return_value = {"api_key": "sk_test_123"}

        # Stripe returns newest first — d1 is newer, d2 is older
        d1 = _make_stripe_dispute("dp_new", amount=5000)
        d1.created = 1700000099  # Newest
        d2 = _make_stripe_dispute("dp_old", amount=3000)
        d2.created = 1700000001  # Oldest

        mock_stripe.Payout.list.return_value = _FakeListResult([])
        mock_stripe.BalanceTransaction.list.return_value = _FakeListResult([])
        mock_stripe.Dispute.list.return_value = _FakeListResult([d1, d2])

        db = MagicMock()
        conn = _make_connection("stripe")
        db.execute.return_value.scalar_one.return_value = conn

        with (
            patch("app.services.ingestion.stripe_sync.load_cursor", return_value=None),
            patch("app.services.ingestion.stripe_sync.save_cursor") as mock_save,
            patch("app.services.ingestion.stripe_sync.upsert_canonical"),
        ):
            from app.services.ingestion.stripe_sync import sync_stripe

            result = sync_stripe(db, str(conn.id), str(conn.tenant_id))

        assert result["disputes_synced"] == 2
        # Cursor should save the NEWEST (highest) timestamp
        dispute_save_call = [c for c in mock_save.call_args_list if "stripe_disputes" in str(c)]
        assert len(dispute_save_call) == 1
        saved_value = dispute_save_call[0].args[3] if dispute_save_call[0].args else dispute_save_call[0][0][3]
        assert saved_value == "1700000099"  # The newest, not 1700000001


# ---------------------------------------------------------------------------
# Stripe payout status-refresh tests (B2)
#
# The incremental sync's cursor is keyed on Stripe's `created` timestamp, which
# never changes when a payout transitions status (e.g. in_transit -> paid) — so
# once synced, non-terminal payouts are never revisited and go stale (verified
# live 2026-07-24: 10/10 sampled non-terminal payouts were `paid` at Stripe but
# `in_transit` in the mirror). ``refresh_payout_statuses`` re-fetches non-terminal
# rows every sync cycle and updates status/arrival_date when Stripe disagrees.
# ---------------------------------------------------------------------------


class TestStripeStatusRefresh:
    @patch("app.services.ingestion.stripe_sync._count_active_stripe_connections", return_value=1)
    @patch("app.services.ingestion.stripe_sync.stripe")
    def test_stale_in_transit_flips_to_paid(self, mock_stripe, mock_count):
        """A stale in_transit row whose Stripe truth is now paid gets updated."""
        row = _make_payout_row(source_id="po_123", status="in_transit", arrival_date=date(2026, 6, 1))

        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = [row]

        retrieved = _make_stripe_payout(payout_id="po_123", status="paid")
        retrieved.arrival_date = 1719878400  # 2024-07-02 00:00:00 UTC (midnight boundary)
        mock_stripe.Payout.retrieve.return_value = retrieved

        from app.services.ingestion.stripe_sync import refresh_payout_statuses

        result = refresh_payout_statuses(db, "conn-1", str(uuid.uuid4()))

        mock_stripe.Payout.retrieve.assert_called_once_with("po_123")
        assert row.status == "paid"
        assert row.arrival_date == datetime.fromtimestamp(1719878400, tz=timezone.utc).date()
        assert result == {"checked": 1, "updated": 1, "errors": 0}
        db.commit.assert_called()

    @patch("app.services.ingestion.stripe_sync._count_active_stripe_connections", return_value=1)
    @patch("app.services.ingestion.stripe_sync.stripe")
    def test_pending_past_arrival_flips(self, mock_stripe, mock_count):
        """A pending row past its expected arrival flips when Stripe reports paid."""
        row = _make_payout_row(source_id="po_456", status="pending", arrival_date=date(2026, 5, 1))

        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = [row]

        retrieved = _make_stripe_payout(payout_id="po_456", status="paid")
        retrieved.arrival_date = 1714521600  # 2024-05-01 00:00:00 UTC (midnight boundary)
        mock_stripe.Payout.retrieve.return_value = retrieved

        from app.services.ingestion.stripe_sync import refresh_payout_statuses

        result = refresh_payout_statuses(db, "conn-1", str(uuid.uuid4()))

        assert row.status == "paid"
        assert row.arrival_date == datetime.fromtimestamp(1714521600, tz=timezone.utc).date()
        assert result == {"checked": 1, "updated": 1, "errors": 0}

    @patch("app.services.ingestion.stripe_sync._count_active_stripe_connections", return_value=1)
    @patch("app.services.ingestion.stripe_sync.stripe")
    def test_terminal_rows_are_not_re_fetched(self, mock_stripe, mock_count):
        """The refresh query filters to non-terminal statuses only — paid/failed/
        canceled rows must never even be selected, let alone re-fetched from Stripe."""
        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = []

        from app.services.ingestion.stripe_sync import refresh_payout_statuses

        refresh_payout_statuses(db, "conn-1", str(uuid.uuid4()))

        # Find the row-select statement among the db.execute calls (there's also
        # a SET LOCAL tenant-context statement, not a Select) and inspect its
        # bound parameter values (not literal-rendered SQL, which chokes on the
        # UUID column type under the generic str-compiler) to prove the status
        # filter is non-terminal-only.
        stmt = next(c.args[0] for c in db.execute.call_args_list if isinstance(c.args[0], Select))
        bound_values = set()
        for value in stmt.compile().params.values():
            if isinstance(value, (list, tuple, set)):
                bound_values.update(value)
            else:
                bound_values.add(value)
        assert "pending" in bound_values
        assert "in_transit" in bound_values
        assert "paid" not in bound_values
        assert "failed" not in bound_values
        assert "canceled" not in bound_values

        mock_stripe.Payout.retrieve.assert_not_called()

    @patch("app.services.ingestion.stripe_sync._count_active_stripe_connections", return_value=1)
    @patch("app.services.ingestion.stripe_sync.stripe")
    def test_query_is_capped_and_ordered_oldest_first(self, mock_stripe, mock_count):
        """Bounds the per-cycle backlog drain (200/cycle; today's real backlog is
        ~38) and processes oldest arrival_date first so a larger backlog drains
        across cycles instead of trying to do it all in one pass."""
        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = []

        from app.services.ingestion.stripe_sync import refresh_payout_statuses

        refresh_payout_statuses(db, "conn-1", str(uuid.uuid4()))

        stmt = next(c.args[0] for c in db.execute.call_args_list if isinstance(c.args[0], Select))
        assert stmt._limit_clause.value == 200
        assert "ORDER BY payouts.arrival_date ASC" in str(stmt)

    @patch("app.services.ingestion.stripe_sync._count_active_stripe_connections", return_value=2)
    @patch("app.services.ingestion.stripe_sync.stripe")
    def test_multi_connection_tenant_skips_refresh(self, mock_stripe, mock_count):
        """Payout has no connection_id column, and the Stripe key is per-connection —
        a tenant with more than one active Stripe connection can't be safely scoped,
        so the whole pass is skipped (and warned) rather than risk polling one
        connection's payouts with another's key."""
        from structlog.testing import capture_logs

        row = _make_payout_row(source_id="po_123", status="in_transit")
        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = [row]

        from app.services.ingestion.stripe_sync import refresh_payout_statuses

        with capture_logs() as logs:
            result = refresh_payout_statuses(db, "conn-1", str(uuid.uuid4()))

        assert result == {"checked": 0, "updated": 0, "errors": 0, "skipped": True}
        mock_stripe.Payout.retrieve.assert_not_called()

        warnings = [e for e in logs if e.get("event") == "stripe_sync.status_refresh.skipped_multi_connection"]
        assert warnings, f"expected a skipped_multi_connection warning, got: {logs}"
        assert warnings[0]["active_stripe_connections"] == 2

    def test_count_active_stripe_connections_filters_tenant_provider_and_status(self):
        """Direct check of the guard query's filter shape, independent of whether
        the skip path is exercised."""
        from app.services.ingestion.stripe_sync import _count_active_stripe_connections

        db = MagicMock()
        db.execute.return_value.scalar_one.return_value = 1
        tenant_id = str(uuid.uuid4())

        result = _count_active_stripe_connections(db, tenant_id)

        assert result == 1
        stmt = db.execute.call_args_list[0].args[0]
        bound_values = set()
        for value in stmt.compile().params.values():
            if isinstance(value, (list, tuple, set)):
                bound_values.update(value)
            else:
                bound_values.add(value)
        assert tenant_id in bound_values
        assert "stripe" in bound_values
        assert "active" in bound_values
        assert "healthy" in bound_values

    @patch("app.services.ingestion.stripe_sync._count_active_stripe_connections", return_value=1)
    @patch("app.services.ingestion.stripe_sync.stripe")
    def test_per_row_stripe_error_is_skipped_and_logged_without_failing_sync(self, mock_stripe, mock_count):
        """A deleted/inaccessible payout at Stripe must not wedge the whole pass."""
        from structlog.testing import capture_logs

        row_bad = _make_payout_row(source_id="po_bad", status="in_transit")
        row_ok = _make_payout_row(source_id="po_ok", status="pending")

        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = [row_bad, row_ok]

        retrieved_ok = _make_stripe_payout(payout_id="po_ok", status="paid")
        retrieved_ok.arrival_date = 1700500000

        mock_stripe.Payout.retrieve.side_effect = [
            RuntimeError("simulated Stripe error (deleted payout)"),
            retrieved_ok,
        ]

        from app.services.ingestion.stripe_sync import refresh_payout_statuses

        with capture_logs() as logs:
            result = refresh_payout_statuses(db, "conn-1", str(uuid.uuid4()))

        assert result == {"checked": 2, "updated": 1, "errors": 1}
        assert row_ok.status == "paid"
        assert row_bad.status == "in_transit"  # untouched after the failed fetch

        warnings = [
            e
            for e in logs
            if e.get("log_level") == "warning" and e.get("event") == "stripe_sync.status_refresh.fetch_failed"
        ]
        assert warnings, f"expected a fetch_failed warning, got: {logs}"
        assert warnings[0]["payout_source_id"] == "po_bad"

    @patch("app.services.ingestion.stripe_sync._count_active_stripe_connections", return_value=1)
    @patch("app.services.ingestion.stripe_sync.stripe")
    def test_row_level_parse_error_is_caught_not_just_retrieve_errors(self, mock_stripe, mock_count):
        """The per-row try/except must cover the whole body (parse/compare/assign),
        not just the Payout.retrieve() call — a malformed arrival_date on an
        otherwise-successful retrieve must not wedge the pass."""
        from structlog.testing import capture_logs

        row_bad = _make_payout_row(source_id="po_bad_epoch", status="in_transit")
        row_ok = _make_payout_row(source_id="po_ok", status="pending")

        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = [row_bad, row_ok]

        retrieved_bad = _make_stripe_payout(payout_id="po_bad_epoch", status="paid")
        retrieved_bad.arrival_date = "not-an-epoch"  # retrieve succeeds; parsing must fail

        retrieved_ok = _make_stripe_payout(payout_id="po_ok", status="paid")
        retrieved_ok.arrival_date = 1700500000

        mock_stripe.Payout.retrieve.side_effect = [retrieved_bad, retrieved_ok]

        from app.services.ingestion.stripe_sync import refresh_payout_statuses

        with capture_logs() as logs:
            result = refresh_payout_statuses(db, "conn-1", str(uuid.uuid4()))

        assert result == {"checked": 2, "updated": 1, "errors": 1}
        assert row_bad.status == "in_transit"  # untouched — parsing blew up before assignment
        assert row_ok.status == "paid"

        warnings = [e for e in logs if e.get("event") == "stripe_sync.status_refresh.fetch_failed"]
        assert any(w.get("payout_source_id") == "po_bad_epoch" for w in warnings)

    @patch("app.services.ingestion.stripe_sync._count_active_stripe_connections", return_value=1)
    @patch("app.services.ingestion.stripe_sync.stripe")
    def test_summary_counts_and_log_event(self, mock_stripe, mock_count):
        from structlog.testing import capture_logs

        row1 = _make_payout_row(source_id="po_1", status="in_transit")
        row2 = _make_payout_row(source_id="po_2", status="pending")
        row3 = _make_payout_row(source_id="po_3", status="in_transit")

        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = [row1, row2, row3]

        unchanged1 = _make_stripe_payout(payout_id="po_1", status="in_transit")
        unchanged1.arrival_date = None
        unchanged2 = _make_stripe_payout(payout_id="po_2", status="pending")
        unchanged2.arrival_date = None
        changed3 = _make_stripe_payout(payout_id="po_3", status="paid")
        changed3.arrival_date = 1700600000

        mock_stripe.Payout.retrieve.side_effect = [unchanged1, unchanged2, changed3]

        from app.services.ingestion.stripe_sync import refresh_payout_statuses

        with capture_logs() as logs:
            result = refresh_payout_statuses(db, "conn-1", str(uuid.uuid4()))

        assert result == {"checked": 3, "updated": 1, "errors": 0}

        summaries = [e for e in logs if e.get("event") == "stripe_sync.status_refresh"]
        assert summaries, f"expected a status_refresh summary log, got: {logs}"
        assert summaries[0]["checked"] == 3
        assert summaries[0]["updated"] == 1
        assert summaries[0]["errors"] == 0

    @patch("app.services.ingestion.stripe_sync._count_active_stripe_connections", return_value=1)
    @patch("app.services.ingestion.stripe_sync.stripe")
    def test_batch_commit_cadence(self, mock_stripe, mock_count):
        """Commits every ~10 rows (Supabase 2-min statement timeout), plus a trailing commit."""
        rows = [_make_payout_row(source_id=f"po_{i}", status="in_transit") for i in range(23)]

        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = rows

        def _retrieve(source_id):
            return _make_stripe_payout(payout_id=source_id, status="in_transit")

        mock_stripe.Payout.retrieve.side_effect = _retrieve

        from app.services.ingestion.stripe_sync import refresh_payout_statuses

        refresh_payout_statuses(db, "conn-1", str(uuid.uuid4()))

        # Batch commit at row 10 and row 20, plus one trailing commit after the loop.
        assert db.commit.call_count == 3

    @patch("app.services.ingestion.stripe_sync._count_active_stripe_connections", return_value=1)
    @patch("app.services.ingestion.stripe_sync.stripe")
    def test_commit_boundary_not_skipped_when_row_errors(self, mock_stripe, mock_count):
        """A row error landing exactly on the batch-commit boundary must not skip
        that periodic commit — the old code's `continue` on a fetch error jumped
        straight past the boundary check, deferring the commit to the next one."""
        rows = [_make_payout_row(source_id=f"po_{i}", status="in_transit") for i in range(10)]

        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = rows

        def _retrieve(source_id):
            if source_id == "po_9":  # the 10th row processed — lands on the boundary
                raise RuntimeError("simulated Stripe error")
            return _make_stripe_payout(payout_id=source_id, status="in_transit")

        mock_stripe.Payout.retrieve.side_effect = _retrieve

        from app.services.ingestion.stripe_sync import refresh_payout_statuses

        refresh_payout_statuses(db, "conn-1", str(uuid.uuid4()))

        # Boundary commit at row 10 (despite its error) + trailing commit = 2.
        assert db.commit.call_count == 2

    @patch("app.services.ingestion.stripe_sync._count_active_stripe_connections", return_value=1)
    @patch("app.services.ingestion.stripe_sync.stripe")
    def test_reestablishes_tenant_context_after_commits(self, mock_stripe, mock_count):
        """SET LOCAL is transaction-scoped, and the payouts sync loop above already
        committed several times before this pass runs — context must be
        re-established at entry, and again after each periodic mid-loop commit
        (same reason: each commit ends the transaction SET LOCAL was scoped to)."""
        tenant_id = str(uuid.uuid4())
        rows = [_make_payout_row(source_id=f"po_{i}", status="in_transit") for i in range(12)]

        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = rows

        def _retrieve(source_id):
            return _make_stripe_payout(payout_id=source_id, status="in_transit")

        mock_stripe.Payout.retrieve.side_effect = _retrieve

        from app.services.ingestion.stripe_sync import refresh_payout_statuses

        refresh_payout_statuses(db, "conn-1", tenant_id)

        expected_sql = f"SET LOCAL app.current_tenant_id = '{tenant_id}'"
        set_local_calls = [c for c in db.execute.call_args_list if str(c.args[0]) == expected_sql]
        # Once before the row query, once more after the row-10 mid-loop commit.
        assert len(set_local_calls) == 2

    @patch("app.services.ingestion.stripe_sync.decrypt_credentials")
    @patch("app.services.ingestion.stripe_sync.stripe")
    @patch("app.services.ingestion.stripe_sync._count_active_stripe_connections", return_value=1)
    def test_sync_stripe_runs_status_refresh_even_with_no_new_payouts(self, mock_count, mock_stripe, mock_decrypt):
        """The refresh pass must run every cycle, even when the incremental fetch finds nothing new."""
        mock_decrypt.return_value = {"api_key": "sk_test_123"}

        mock_stripe.Payout.list.return_value = _FakeListResult([])
        mock_stripe.BalanceTransaction.list.return_value = _FakeListResult([])
        mock_stripe.Dispute.list.return_value = _FakeListResult([])

        stale_row = _make_payout_row(source_id="po_stale", status="in_transit")
        retrieved = _make_stripe_payout(payout_id="po_stale", status="paid")
        retrieved.arrival_date = 1700700000
        mock_stripe.Payout.retrieve.return_value = retrieved

        db = MagicMock()
        conn = _make_connection("stripe")
        db.execute.return_value.scalar_one.return_value = conn
        db.execute.return_value.scalar_one_or_none.return_value = None
        db.execute.return_value.scalars.return_value.all.return_value = [stale_row]

        with (
            patch("app.services.ingestion.stripe_sync.load_cursor", return_value=None),
            patch("app.services.ingestion.stripe_sync.save_cursor"),
            patch("app.services.ingestion.stripe_sync.upsert_canonical"),
        ):
            from app.services.ingestion.stripe_sync import sync_stripe

            result = sync_stripe(db, str(conn.id), str(conn.tenant_id))

        assert result["payouts_synced"] == 0
        assert stale_row.status == "paid"
        assert stale_row.arrival_date == datetime.fromtimestamp(1700700000, tz=timezone.utc).date()
        assert result["payouts_refresh_checked"] == 1
        assert result["payouts_refresh_updated"] == 1
        assert result["payouts_refresh_errors"] == 0

    @patch("app.services.ingestion.stripe_sync.decrypt_credentials")
    @patch("app.services.ingestion.stripe_sync.stripe")
    @patch("app.services.ingestion.stripe_sync.refresh_payout_statuses")
    def test_refresh_pass_failure_does_not_fail_sync(self, mock_refresh, mock_stripe, mock_decrypt):
        """No refresh-pass failure — including a commit error deep inside it — may
        ever fail the surrounding sync (or a recon run that depends on it)."""
        from structlog.testing import capture_logs

        mock_decrypt.return_value = {"api_key": "sk_test_123"}
        mock_stripe.Payout.list.return_value = _FakeListResult([])
        mock_stripe.BalanceTransaction.list.return_value = _FakeListResult([])
        mock_stripe.Dispute.list.return_value = _FakeListResult([])
        mock_refresh.side_effect = RuntimeError("simulated commit failure during refresh")

        db = MagicMock()
        conn = _make_connection("stripe")
        db.execute.return_value.scalar_one.return_value = conn
        db.execute.return_value.scalar_one_or_none.return_value = None

        with (
            patch("app.services.ingestion.stripe_sync.load_cursor", return_value=None),
            patch("app.services.ingestion.stripe_sync.save_cursor"),
            patch("app.services.ingestion.stripe_sync.upsert_canonical"),
        ):
            from app.services.ingestion.stripe_sync import sync_stripe

            with capture_logs() as logs:
                result = sync_stripe(db, str(conn.id), str(conn.tenant_id))

        assert result["payouts_synced"] == 0
        assert result["payouts_refresh_checked"] == 0
        assert result["payouts_refresh_updated"] == 0
        assert result["payouts_refresh_errors"] == 0
        db.rollback.assert_called()

        failures = [e for e in logs if e.get("event") == "stripe_sync.status_refresh.pass_failed"]
        assert failures, f"expected a pass_failed warning, got: {logs}"


class TestStripeEpochConversion:
    def test_epoch_near_midnight_utc_does_not_shift_with_host_tz(self):
        """date.fromtimestamp() uses the HOST's local timezone, which can shift an
        epoch near midnight UTC to the wrong calendar day depending on where the
        process runs. _stripe_epoch_to_date must always go through the epoch's
        UTC wall-clock date, regardless of the host's local timezone."""
        import os
        import time

        from app.services.ingestion.stripe_sync import _stripe_epoch_to_date

        epoch = int(datetime(2024, 1, 1, 0, 30, tzinfo=timezone.utc).timestamp())

        old_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "US/Pacific"
            time.tzset()
            result = _stripe_epoch_to_date(epoch)
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

        assert result == date(2024, 1, 1)

    def test_none_epoch_returns_none(self):
        from app.services.ingestion.stripe_sync import _stripe_epoch_to_date

        assert _stripe_epoch_to_date(None) is None


# ---------------------------------------------------------------------------
# Shopify sync tests
# ---------------------------------------------------------------------------


class TestShopifySync:
    @patch("app.services.ingestion.shopify_sync.decrypt_credentials")
    @patch("app.services.ingestion.shopify_sync.httpx.Client")
    def test_order_sync_basic(self, mock_client_class, mock_decrypt):
        mock_decrypt.return_value = {
            "access_token": "shpat_test",
            "shop_domain": "test-shop.myshopify.com",
        }

        order = {
            "id": 12345,
            "order_number": 1001,
            "email": "test@example.com",
            "currency": "USD",
            "total_price": "99.99",
            "subtotal_price": "89.99",
            "total_tax": "10.00",
            "total_discounts": "0.00",
            "financial_status": "paid",
            "updated_at": "2024-01-15T10:00:00Z",
            "refunds": [],
        }

        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)

        # Orders response
        orders_response = MagicMock()
        orders_response.json.return_value = {"orders": [order]}
        orders_response.headers = {}
        orders_response.raise_for_status = MagicMock()

        # Transactions response (empty)
        txn_response = MagicMock()
        txn_response.json.return_value = {"transactions": []}
        txn_response.raise_for_status = MagicMock()

        mock_client.get.side_effect = [orders_response, txn_response]

        db = MagicMock()
        conn = _make_connection("shopify")
        db.execute.return_value.scalar_one.return_value = conn
        db.execute.return_value.scalar_one_or_none.return_value = None

        with (
            patch("app.services.ingestion.shopify_sync.load_cursor", return_value=None),
            patch("app.services.ingestion.shopify_sync.save_cursor"),
            patch("app.services.ingestion.shopify_sync.upsert_canonical") as mock_upsert,
        ):
            from app.services.ingestion.shopify_sync import sync_shopify

            result = sync_shopify(db, str(conn.id), str(conn.tenant_id))

        assert result["orders_synced"] == 1
        assert result["refunds_synced"] == 0
        mock_upsert.assert_called()

    @patch("app.services.ingestion.shopify_sync.decrypt_credentials")
    @patch("app.services.ingestion.shopify_sync.httpx.Client")
    def test_order_with_refunds(self, mock_client_class, mock_decrypt):
        mock_decrypt.return_value = {
            "access_token": "shpat_test",
            "shop_domain": "test-shop.myshopify.com",
        }

        order = {
            "id": 12345,
            "order_number": 1001,
            "email": "test@example.com",
            "currency": "USD",
            "total_price": "99.99",
            "subtotal_price": "89.99",
            "total_tax": "10.00",
            "total_discounts": "0.00",
            "financial_status": "refunded",
            "updated_at": "2024-01-15T10:00:00Z",
            "refunds": [
                {
                    "id": 5001,
                    "note": "Customer requested refund",
                    "refund_line_items": [
                        {"subtotal": "50.00"},
                        {"subtotal": "39.99"},
                    ],
                }
            ],
        }

        mock_client = MagicMock()
        mock_client_class.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_class.return_value.__exit__ = MagicMock(return_value=False)

        orders_response = MagicMock()
        orders_response.json.return_value = {"orders": [order]}
        orders_response.headers = {}
        orders_response.raise_for_status = MagicMock()

        txn_response = MagicMock()
        txn_response.json.return_value = {"transactions": []}
        txn_response.raise_for_status = MagicMock()

        mock_client.get.side_effect = [orders_response, txn_response]

        db = MagicMock()
        conn = _make_connection("shopify")
        db.execute.return_value.scalar_one.return_value = conn
        db.execute.return_value.scalar_one_or_none.return_value = None

        with (
            patch("app.services.ingestion.shopify_sync.load_cursor", return_value=None),
            patch("app.services.ingestion.shopify_sync.save_cursor"),
            patch("app.services.ingestion.shopify_sync.upsert_canonical") as mock_upsert,
        ):
            from app.services.ingestion.shopify_sync import sync_shopify

            result = sync_shopify(db, str(conn.id), str(conn.tenant_id))

        assert result["orders_synced"] == 1
        assert result["refunds_synced"] == 1
        # Should have 2 upsert calls: 1 order + 1 refund
        assert mock_upsert.call_count == 2


# ---------------------------------------------------------------------------
# Cursor state tests
# ---------------------------------------------------------------------------


class TestCursorState:
    def test_load_cursor_returns_none_when_empty(self):
        db = MagicMock()
        db.execute.return_value.scalar_one_or_none.return_value = None
        result = load_cursor(db, uuid.uuid4(), "stripe_payouts")
        assert result is None

    def test_load_cursor_returns_value(self):
        db = MagicMock()
        db.execute.return_value.scalar_one_or_none.return_value = "1700000000"
        result = load_cursor(db, uuid.uuid4(), "stripe_payouts")
        assert result == "1700000000"

    def test_save_cursor_calls_execute(self):
        db = MagicMock()
        save_cursor(db, uuid.uuid4(), "stripe_payouts", "1700000000")
        db.execute.assert_called_once()
