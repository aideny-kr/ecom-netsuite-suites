"""Tests for the metered billing tollbooth and Stripe sync task."""

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.services.chat.billing import calculate_cost, deduct_chat_credits

# ── calculate_cost tests ──


class TestCalculateCost:
    def test_haiku_tier(self):
        assert calculate_cost("claude-haiku-4-5-20251001") == 1

    def test_flash_tier(self):
        assert calculate_cost("gemini-2.5-flash") == 1

    def test_nano_tier(self):
        assert calculate_cost("gpt-5-nano") == 1

    def test_mini_tier(self):
        assert calculate_cost("gpt-4.1-mini") == 1

    def test_lite_tier(self):
        assert calculate_cost("gemini-2.5-flash-lite") == 1

    def test_sonnet_tier(self):
        assert calculate_cost("claude-sonnet-4-20250514") == 2

    def test_pro_tier(self):
        assert calculate_cost("gemini-2.5-pro") == 2

    def test_opus_tier(self):
        assert calculate_cost("claude-opus-4-6") == 3

    def test_unknown_model_defaults_to_1(self):
        assert calculate_cost("some-unknown-model-xyz") == 1

    def test_case_insensitive(self):
        assert calculate_cost("Claude-SONNET-4") == 2
        assert calculate_cost("GPT-5-NANO") == 1


# ── deduct_chat_credits tests ──


class TestDeductChatCredits:
    @pytest.fixture
    def mock_wallet(self):
        wallet = MagicMock()
        wallet.base_credits_remaining = 100
        wallet.metered_credits_used = 0
        return wallet

    @pytest.fixture
    def mock_db(self, mock_wallet):
        db = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = mock_wallet
        db.execute = MagicMock(return_value=result)
        return db

    @pytest.mark.asyncio
    async def test_deduct_from_base_credits(self, mock_db, mock_wallet):
        # Make db.execute async
        async def mock_execute(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = mock_wallet
            return result

        mock_db.execute = mock_execute
        tenant_id = uuid.uuid4()

        result = await deduct_chat_credits(mock_db, tenant_id, "claude-sonnet-4-20250514")

        assert result is not None
        assert result["cost"] == 2
        assert mock_wallet.base_credits_remaining == 98
        assert mock_wallet.metered_credits_used == 0

    @pytest.mark.asyncio
    async def test_overage_spillover(self, mock_db, mock_wallet):
        mock_wallet.base_credits_remaining = 1

        async def mock_execute(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = mock_wallet
            return result

        mock_db.execute = mock_execute
        tenant_id = uuid.uuid4()

        result = await deduct_chat_credits(mock_db, tenant_id, "claude-sonnet-4-20250514")

        assert result is not None
        assert result["cost"] == 2
        assert mock_wallet.base_credits_remaining == 0
        assert mock_wallet.metered_credits_used == 1

    @pytest.mark.asyncio
    async def test_full_overage(self, mock_db, mock_wallet):
        mock_wallet.base_credits_remaining = 0

        async def mock_execute(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = mock_wallet
            return result

        mock_db.execute = mock_execute
        tenant_id = uuid.uuid4()

        result = await deduct_chat_credits(mock_db, tenant_id, "claude-opus-4-6")

        assert result is not None
        assert result["cost"] == 3
        assert mock_wallet.base_credits_remaining == 0
        assert mock_wallet.metered_credits_used == 3

    @pytest.mark.asyncio
    async def test_no_wallet_returns_none(self, mock_db):
        async def mock_execute(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            return result

        mock_db.execute = mock_execute
        tenant_id = uuid.uuid4()

        result = await deduct_chat_credits(mock_db, tenant_id, "claude-sonnet-4-20250514")
        assert result is None


# ── Stripe sync task tests ──


class TestBillingSyncTask:
    def test_sync_no_wallets(self):
        """When no wallets need syncing, returns zeros."""
        from app.workers.tasks.billing_sync import sync_metered_billing_to_stripe

        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        with patch("sqlalchemy.orm.Session", return_value=mock_session), patch("app.workers.base_task.sync_engine"):
            result = sync_metered_billing_to_stripe()

        assert result["synced"] == 0
        assert result["errors"] == 0

    def test_sync_reports_delta_to_stripe(self):
        """Wallets with unsynced credits should report delta to Stripe."""
        from app.workers.tasks.billing_sync import sync_metered_billing_to_stripe

        wallet = MagicMock()
        wallet.tenant_id = uuid.uuid4()
        wallet.stripe_subscription_item_id = "si_abc123"
        wallet.metered_credits_used = 50
        wallet.last_synced_metered_credits = 30

        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [wallet]
        mock_session.execute.return_value = mock_result
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_stripe = MagicMock()
        with (
            patch("sqlalchemy.orm.Session", return_value=mock_session),
            patch("app.workers.base_task.sync_engine"),
            patch.dict("sys.modules", {"stripe": mock_stripe}),
        ):
            mock_stripe.api_key = None
            mock_stripe.SubscriptionItem.create_usage_record = MagicMock()

            result = sync_metered_billing_to_stripe()

        assert result["synced"] == 1
        assert result["errors"] == 0
        assert wallet.last_synced_metered_credits == 50
        mock_stripe.SubscriptionItem.create_usage_record.assert_called_once_with(
            "si_abc123", quantity=20, action="increment"
        )
