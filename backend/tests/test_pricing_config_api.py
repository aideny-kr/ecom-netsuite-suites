"""Tests for pricing config service — get/upsert operations."""
import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from app.services import pricing_config_service


class TestPricingConfigService:

    @pytest.mark.asyncio
    async def test_get_config_returns_none_when_empty(self):
        """get_config returns None when no row exists for the tenant."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await pricing_config_service.get_config(
            mock_db, uuid.UUID("00000000-0000-0000-0000-000000000001")
        )
        assert result is None
        mock_db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_config_returns_model_when_found(self):
        """get_config returns the existing ORM model when a row is found."""
        mock_db = AsyncMock()
        fake_config = MagicMock()
        fake_config.config = {"base_currency": "USD", "currencies": {}}
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = fake_config
        mock_db.execute.return_value = mock_result

        result = await pricing_config_service.get_config(
            mock_db, uuid.uuid4()
        )
        assert result is fake_config
        assert result.config["base_currency"] == "USD"

    @pytest.mark.asyncio
    async def test_upsert_creates_new_when_none_exists(self):
        """upsert_config calls db.add when no existing config row is found."""
        mock_db = AsyncMock()
        # get_config (called inside upsert_config) returns None
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        config_data = {"base_currency": "USD", "currencies": {}}
        result = await pricing_config_service.upsert_config(
            mock_db, uuid.uuid4(), config_data, uuid.uuid4()
        )

        # A new model should have been added to the session
        mock_db.add.assert_called_once()
        added_model = mock_db.add.call_args[0][0]
        assert added_model.config == config_data

    @pytest.mark.asyncio
    async def test_upsert_updates_existing_in_place(self):
        """upsert_config mutates existing row without calling db.add."""
        mock_db = AsyncMock()
        existing = MagicMock()
        existing.config = {"old": True}
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_db.execute.return_value = mock_result

        new_config_data = {"base_currency": "USD", "currencies": {"GBP": {}}}
        updated_by = uuid.uuid4()
        result = await pricing_config_service.upsert_config(
            mock_db, uuid.uuid4(), new_config_data, updated_by
        )

        # Should update in-place, not call db.add
        assert existing.config == new_config_data
        assert existing.updated_by == updated_by
        mock_db.add.assert_not_called()
        # Should return the existing object
        assert result is existing

    @pytest.mark.asyncio
    async def test_upsert_new_config_stores_tenant_id(self):
        """upsert_config passes tenant_id correctly to the new model."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        tenant_id = uuid.uuid4()
        config_data = {"base_currency": "EUR", "currencies": {}}
        await pricing_config_service.upsert_config(
            mock_db, tenant_id, config_data, uuid.uuid4()
        )

        added_model = mock_db.add.call_args[0][0]
        assert added_model.tenant_id == tenant_id

    @pytest.mark.asyncio
    async def test_upsert_new_config_stores_updated_by(self):
        """upsert_config passes updated_by correctly to the new model."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        updated_by = uuid.uuid4()
        await pricing_config_service.upsert_config(
            mock_db, uuid.uuid4(), {"base_currency": "USD", "currencies": {}}, updated_by
        )

        added_model = mock_db.add.call_args[0][0]
        assert added_model.updated_by == updated_by
