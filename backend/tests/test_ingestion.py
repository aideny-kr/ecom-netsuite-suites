"""Tests for Stripe and Shopify ingestion services with mocked APIs."""

import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.services.ingestion.base import load_cursor, save_cursor, upsert_canonical


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

        with patch("app.services.ingestion.stripe_sync.load_cursor", return_value=None), \
             patch("app.services.ingestion.stripe_sync.save_cursor") as mock_save, \
             patch("app.services.ingestion.stripe_sync.upsert_canonical") as mock_upsert:
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

        with patch("app.services.ingestion.stripe_sync.load_cursor", return_value=None), \
             patch("app.services.ingestion.stripe_sync.save_cursor") as mock_save, \
             patch("app.services.ingestion.stripe_sync.upsert_canonical") as mock_upsert:
            from app.services.ingestion.stripe_sync import sync_stripe

            result = sync_stripe(db, str(conn.id), str(conn.tenant_id))

        assert result["disputes_synced"] == 1
        mock_save.assert_called()


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

        with patch("app.services.ingestion.shopify_sync.load_cursor", return_value=None), \
             patch("app.services.ingestion.shopify_sync.save_cursor") as mock_save, \
             patch("app.services.ingestion.shopify_sync.upsert_canonical") as mock_upsert:
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

        with patch("app.services.ingestion.shopify_sync.load_cursor", return_value=None), \
             patch("app.services.ingestion.shopify_sync.save_cursor"), \
             patch("app.services.ingestion.shopify_sync.upsert_canonical") as mock_upsert:
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
