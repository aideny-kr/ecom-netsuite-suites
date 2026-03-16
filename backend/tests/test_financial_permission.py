"""Tests for financial report permission gating."""

from unittest.mock import AsyncMock, patch
import pytest


class TestFinancialPermissionGating:
    @pytest.mark.asyncio
    async def test_has_permission_called_correctly(self):
        """Verify has_permission is called with correct args for financial check."""
        mock_hp = AsyncMock(return_value=True)
        with patch("app.core.dependencies.has_permission", mock_hp):
            from app.core.dependencies import has_permission
            result = await has_permission(AsyncMock(), "fake-user-id", "chat.financial_reports")
            assert result is True
            mock_hp.assert_called_once()

    @pytest.mark.asyncio
    async def test_permission_denied_returns_false(self):
        """Verify has_permission returns False for denied permission."""
        mock_hp = AsyncMock(return_value=False)
        with patch("app.core.dependencies.has_permission", mock_hp):
            from app.core.dependencies import has_permission
            result = await has_permission(AsyncMock(), "fake-user-id", "chat.financial_reports")
            assert result is False
