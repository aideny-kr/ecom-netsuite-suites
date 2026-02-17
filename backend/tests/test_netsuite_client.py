"""Tests for NetSuite SuiteQL client (MCP + REST)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.netsuite_client import (
    execute_suiteql,
    execute_suiteql_via_mcp,
    execute_suiteql_via_rest,
)


class TestExecuteSuiteqlViaRest:
    @pytest.mark.asyncio
    async def test_parses_response(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "items": [
                {"id": "1", "name": "Acme"},
                {"id": "2", "name": "Globex"},
            ],
            "totalResults": 2,
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.services.netsuite_client.httpx.AsyncClient") as mock_client:
            client_instance = AsyncMock()
            client_instance.post.return_value = mock_response
            mock_client.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await execute_suiteql_via_rest(
                "token123", "12345-sb1", "SELECT id, name FROM customer", 100
            )

            assert result["columns"] == ["id", "name"]
            assert result["rows"] == [["1", "Acme"], ["2", "Globex"]]
            assert result["row_count"] == 2
            assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_truncated_flag(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "items": [{"id": "1"}],
            "totalResults": 100,
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.services.netsuite_client.httpx.AsyncClient") as mock_client:
            client_instance = AsyncMock()
            client_instance.post.return_value = mock_response
            mock_client.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await execute_suiteql_via_rest("token", "acct", "SELECT id FROM x", 1)
            assert result["truncated"] is True

    @pytest.mark.asyncio
    async def test_empty_response(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"items": [], "totalResults": 0}
        mock_response.raise_for_status = MagicMock()

        with patch("app.services.netsuite_client.httpx.AsyncClient") as mock_client:
            client_instance = AsyncMock()
            client_instance.post.return_value = mock_response
            mock_client.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await execute_suiteql_via_rest("token", "acct", "SELECT id FROM x", 10)
            assert result["columns"] == []
            assert result["rows"] == []
            assert result["row_count"] == 0

    @pytest.mark.asyncio
    async def test_http_error_propagates(self):
        with patch("app.services.netsuite_client.httpx.AsyncClient") as mock_client:
            client_instance = AsyncMock()
            response = MagicMock()
            response.status_code = 401
            client_instance.post.return_value = response
            response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Unauthorized", request=MagicMock(), response=response
            )
            mock_client.return_value.__aenter__ = AsyncMock(return_value=client_instance)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(httpx.HTTPStatusError):
                await execute_suiteql_via_rest("bad_token", "acct", "SELECT 1", 1)


class TestExecuteSuiteqlViaMcp:
    @pytest.mark.asyncio
    async def test_mcp_execution(self):
        """Test MCP path with mocked SDK."""
        from contextlib import asynccontextmanager

        mock_text_block = MagicMock()
        mock_text_block.text = '{"items": [{"id": "1"}]}'
        mock_result = MagicMock()
        mock_result.isError = False
        mock_result.content = [mock_text_block]

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        @asynccontextmanager
        async def fake_streamablehttp_client(**kwargs):
            yield MagicMock(), MagicMock(), MagicMock()

        mock_session_cls = MagicMock()
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        session_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = session_cm

        with (
            patch("mcp.client.streamable_http.streamablehttp_client", side_effect=fake_streamablehttp_client),
            patch("mcp.ClientSession", mock_session_cls),
        ):
            result = await execute_suiteql_via_mcp("token", "12345", "SELECT id FROM x", 10)

            assert result["columns"] == ["id"]
            assert result["rows"] == [["1"]]
            assert result["row_count"] == 1


class TestExecuteSuiteqlFallback:
    @pytest.mark.asyncio
    async def test_falls_back_to_rest_on_mcp_failure(self):
        rest_result = {
            "columns": ["id"],
            "rows": [["1"]],
            "row_count": 1,
            "truncated": False,
        }

        with (
            patch(
                "app.services.netsuite_client.execute_suiteql_via_mcp",
                new_callable=AsyncMock,
                side_effect=Exception("MCP connection refused"),
            ),
            patch(
                "app.services.netsuite_client.execute_suiteql_via_rest",
                new_callable=AsyncMock,
                return_value=rest_result,
            ) as mock_rest,
        ):
            result = await execute_suiteql("token", "acct", "SELECT id FROM x", 10)

            assert result == rest_result
            mock_rest.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mcp_success_skips_rest(self):
        mcp_result = {
            "columns": ["id"],
            "rows": [["mcp_1"]],
            "row_count": 1,
            "truncated": False,
        }

        with (
            patch(
                "app.services.netsuite_client.execute_suiteql_via_mcp",
                new_callable=AsyncMock,
                return_value=mcp_result,
            ),
            patch(
                "app.services.netsuite_client.execute_suiteql_via_rest",
                new_callable=AsyncMock,
            ) as mock_rest,
        ):
            result = await execute_suiteql("token", "acct", "SELECT id FROM x", 10)

            assert result["rows"] == [["mcp_1"]]
            mock_rest.assert_not_awaited()
