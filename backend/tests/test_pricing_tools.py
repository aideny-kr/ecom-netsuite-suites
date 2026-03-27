"""Tests for pricing tool executors."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.tools.pricing_tools import (
    _file_svc,
    pricing_config_read_execute,
    pricing_convert_execute,
)


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
        with patch("app.mcp.tools.pricing_tools.get_config", new_callable=AsyncMock, return_value=mock_row), patch.object(
            _file_svc, "get_file", new_callable=AsyncMock, side_effect=ValueError("not found")
        ):
            result = await pricing_convert_execute({"file_id": str(uuid.uuid4())}, mock_context)
        assert result["error"] is True
        assert "File not found" in result["message"]
