"""Tests for pricing tool executors."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.tools.pricing_tools import (
    _file_svc,
    pricing_config_read_execute,
    pricing_config_update_execute,
    pricing_convert_execute,
)
from app.services.pricing_config_defaults import get_default_config


@pytest.fixture
def mock_context():
    return {"db": AsyncMock(), "tenant_id": uuid.uuid4(), "user_id": uuid.uuid4()}


class TestPricingConfigRead:
    @pytest.mark.asyncio
    async def test_no_config(self, mock_context):
        with patch("app.mcp.tools.pricing_tools.get_config", new_callable=AsyncMock, return_value=None):
            result = await pricing_config_read_execute({}, mock_context)
        assert result["error"] is True
        assert "No pricing configuration" in result["message"]

    @pytest.mark.asyncio
    async def test_with_config(self, mock_context):
        mock_row = MagicMock()
        mock_row.config = {
            "base_currency": "USD",
            "eur_fx_rate": "0.92",
            "currencies": {
                "GBP": {
                    "fx_rate": "0.79",
                    "tier": "usd_based",
                    "vat_rate": "0.20",
                    "rounding_rule": "nearest_9",
                }
            },
        }
        with patch("app.mcp.tools.pricing_tools.get_config", new_callable=AsyncMock, return_value=mock_row):
            result = await pricing_config_read_execute({}, mock_context)
        assert result["success"] is True
        assert result["currency_count"] == 1
        assert "GBP" in result["currencies"]
        assert result["base_currency"] == "USD"


class TestPricingConfigUpdate:
    @pytest.mark.asyncio
    async def test_rejects_eur_currency_fx_rate_update_for_eur_based_base(self, mock_context):
        mock_row = MagicMock()
        mock_row.config = get_default_config()

        with (
            patch("app.mcp.tools.pricing_tools.get_config", new_callable=AsyncMock, return_value=mock_row),
            patch("app.mcp.tools.pricing_tools.upsert_config", new_callable=AsyncMock) as upsert_config,
        ):
            result = await pricing_config_update_execute(
                {"updates": {"currencies": {"EUR": {"fx_rate": 1.10}}}},
                mock_context,
            )

        assert result["error"] is True
        assert "top-level eur_fx_rate" in result["message"]
        upsert_config.assert_not_called()
        mock_context["db"].commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_eur_currency_fx_rate_update_before_mutating_other_currency(self, mock_context):
        mock_row = MagicMock()
        mock_row.config = get_default_config()

        with (
            patch("app.mcp.tools.pricing_tools.get_config", new_callable=AsyncMock, return_value=mock_row),
            patch("app.mcp.tools.pricing_tools.upsert_config", new_callable=AsyncMock) as upsert_config,
        ):
            result = await pricing_config_update_execute(
                {
                    "updates": {
                        "currencies": {
                            "GBP": {"fx_rate": 0.81},
                            "EUR": {"fx_rate": 1.10},
                        }
                    }
                },
                mock_context,
            )

        assert result["error"] is True
        assert mock_row.config["currencies"]["GBP"]["fx_rate"] == "0.79"
        upsert_config.assert_not_called()
        mock_context["db"].commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allows_top_level_eur_fx_rate_update(self, mock_context):
        mock_row = MagicMock()
        mock_row.config = get_default_config()

        with (
            patch("app.mcp.tools.pricing_tools.get_config", new_callable=AsyncMock, return_value=mock_row),
            patch("app.mcp.tools.pricing_tools.upsert_config", new_callable=AsyncMock) as upsert_config,
        ):
            result = await pricing_config_update_execute(
                {"updates": {"eur_fx_rate": 1.10}},
                mock_context,
            )

        assert result["success"] is True
        saved_config = upsert_config.await_args.args[2]
        assert saved_config["eur_fx_rate"] == 1.10
        mock_context["db"].commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejects_final_price_literal_as_top_level_eur_fx_rate(self, mock_context):
        mock_row = MagicMock()
        mock_row.config = get_default_config()

        with (
            patch("app.mcp.tools.pricing_tools.get_config", new_callable=AsyncMock, return_value=mock_row),
            patch("app.mcp.tools.pricing_tools.upsert_config", new_callable=AsyncMock) as upsert_config,
        ):
            result = await pricing_config_update_execute(
                {"updates": {"eur_fx_rate": 150}},
                mock_context,
            )

        assert result["error"] is True
        assert "exchange rate" in result["message"]
        assert "final EUR display price" in result["message"]
        upsert_config.assert_not_called()
        mock_context["db"].commit.assert_not_awaited()


class TestPricingConvert:
    @pytest.mark.asyncio
    async def test_no_config(self, mock_context):
        with patch("app.mcp.tools.pricing_tools.get_config", new_callable=AsyncMock, return_value=None):
            result = await pricing_convert_execute({"file_id": str(uuid.uuid4())}, mock_context)
        assert result["error"] is True
        assert "No pricing configuration" in result["message"]

    @pytest.mark.asyncio
    async def test_no_file_id(self, mock_context):
        mock_row = MagicMock()
        mock_row.config = {"base_currency": "USD", "eur_fx_rate": "0.92", "currencies": {}}
        with patch("app.mcp.tools.pricing_tools.get_config", new_callable=AsyncMock, return_value=mock_row):
            result = await pricing_convert_execute({}, mock_context)
        assert result["error"] is True
        assert "file_id" in result["message"]

    @pytest.mark.asyncio
    async def test_file_not_found(self, mock_context):
        mock_row = MagicMock()
        mock_row.config = {"base_currency": "USD", "eur_fx_rate": "0.92", "currencies": {}}
        with (
            patch("app.mcp.tools.pricing_tools.get_config", new_callable=AsyncMock, return_value=mock_row),
            patch.object(_file_svc, "get_file", new_callable=AsyncMock, side_effect=ValueError("not found")),
        ):
            result = await pricing_convert_execute({"file_id": str(uuid.uuid4())}, mock_context)
        assert result["error"] is True
        assert "File not found" in result["message"]
