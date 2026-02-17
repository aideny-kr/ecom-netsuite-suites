"""Tests for the external MCP client service (mcp_client_service.py)."""

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.mcp_client_service import (
    _build_headers,
    call_external_mcp_tool,
    discover_tools,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeConnector:
    server_url: str = "https://example.com/mcp/v1"
    auth_type: str = "none"
    encrypted_credentials: str | None = None


def _mock_mcp(mock_session):
    """Patch streamablehttp_client at its imported location."""

    @asynccontextmanager
    async def fake_streamablehttp_client(**kwargs):
        read_stream = MagicMock()
        write_stream = MagicMock()
        yield read_stream, write_stream, MagicMock()

    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

    transport_patch = patch(
        "app.services.mcp_client_service.streamablehttp_client",
        side_effect=fake_streamablehttp_client,
    )
    session_patch = patch(
        "app.services.mcp_client_service.ClientSession",
        return_value=mock_session_ctx,
    )
    return transport_patch, session_patch


# ---------------------------------------------------------------------------
# Auth header building
# ---------------------------------------------------------------------------


class TestBuildHeaders:
    @pytest.mark.asyncio
    async def test_none_auth(self):
        assert await _build_headers(FakeConnector(auth_type="none")) == {}

    @pytest.mark.asyncio
    async def test_bearer_auth(self):
        with patch("app.services.mcp_client_service.decrypt_credentials") as m:
            m.return_value = {"access_token": "my-token"}
            headers = await _build_headers(FakeConnector(auth_type="bearer", encrypted_credentials="enc"))
        assert headers == {"Authorization": "Bearer my-token"}

    @pytest.mark.asyncio
    async def test_api_key_default_header(self):
        with patch("app.services.mcp_client_service.decrypt_credentials") as m:
            m.return_value = {"api_key": "key-123"}
            headers = await _build_headers(FakeConnector(auth_type="api_key", encrypted_credentials="enc"))
        assert headers == {"X-API-Key": "key-123"}

    @pytest.mark.asyncio
    async def test_api_key_custom_header(self):
        with patch("app.services.mcp_client_service.decrypt_credentials") as m:
            m.return_value = {"api_key": "key-123", "header_name": "X-Custom"}
            headers = await _build_headers(FakeConnector(auth_type="api_key", encrypted_credentials="enc"))
        assert headers == {"X-Custom": "key-123"}

    @pytest.mark.asyncio
    async def test_no_credentials_returns_empty(self):
        assert await _build_headers(FakeConnector(auth_type="bearer", encrypted_credentials=None)) == {}


# ---------------------------------------------------------------------------
# discover_tools
# ---------------------------------------------------------------------------


class TestDiscoverTools:
    @pytest.mark.asyncio
    async def test_discover_returns_tools(self):
        mock_tool = MagicMock(name="ns_runSuiteQL")
        mock_tool.name = "ns_runSuiteQL"
        mock_tool.description = "Run a SuiteQL query"
        mock_tool.inputSchema = {"type": "object"}

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_list_result = MagicMock()
        mock_list_result.tools = [mock_tool]
        mock_session.list_tools = AsyncMock(return_value=mock_list_result)

        tp, sp = _mock_mcp(mock_session)
        with tp, sp:
            tools = await discover_tools(FakeConnector())

        assert len(tools) == 1
        assert tools[0]["name"] == "ns_runSuiteQL"
        assert tools[0]["description"] == "Run a SuiteQL query"

    @pytest.mark.asyncio
    async def test_discover_empty_tools(self):
        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_list_result = MagicMock()
        mock_list_result.tools = []
        mock_session.list_tools = AsyncMock(return_value=mock_list_result)

        tp, sp = _mock_mcp(mock_session)
        with tp, sp:
            tools = await discover_tools(FakeConnector())

        assert tools == []


# ---------------------------------------------------------------------------
# call_external_mcp_tool
# ---------------------------------------------------------------------------


class TestCallExternalMcpTool:
    @pytest.mark.asyncio
    async def test_success_json_response(self):
        block = MagicMock()
        block.text = json.dumps({"items": [{"id": 1}]})

        mock_result = MagicMock(isError=False, content=[block])
        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        tp, sp = _mock_mcp(mock_session)
        with tp, sp:
            result = await call_external_mcp_tool(FakeConnector(), "ns_runSuiteQL", {"q": "SELECT 1"})

        assert result == {"items": [{"id": 1}]}

    @pytest.mark.asyncio
    async def test_error_response(self):
        mock_result = MagicMock(isError=True, content="Something went wrong")
        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        tp, sp = _mock_mcp(mock_session)
        with tp, sp:
            result = await call_external_mcp_tool(FakeConnector(), "bad_tool", {})

        assert "error" in result

    @pytest.mark.asyncio
    async def test_plain_text_response(self):
        block = MagicMock()
        block.text = "Hello, this is plain text"

        mock_result = MagicMock(isError=False, content=[block])
        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        tp, sp = _mock_mcp(mock_session)
        with tp, sp:
            result = await call_external_mcp_tool(FakeConnector(), "some_tool", {})

        assert result == {"result": "Hello, this is plain text"}

    @pytest.mark.asyncio
    async def test_no_content_response(self):
        mock_result = MagicMock(isError=False, content=[])
        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        tp, sp = _mock_mcp(mock_session)
        with tp, sp:
            result = await call_external_mcp_tool(FakeConnector(), "empty_tool", {})

        assert result == {"result": "No content returned"}
