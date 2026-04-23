from unittest.mock import AsyncMock, MagicMock

import pytest


def _mock_db(connector):
    """Build an async-execute-returning-scalars-first mock chain."""
    db = MagicMock()
    scalars_result = MagicMock()
    scalars_result.first.return_value = connector
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    db.execute = AsyncMock(return_value=execute_result)
    return db


@pytest.mark.asyncio
async def test_drive_read_doc_missing_context_returns_error():
    from app.mcp.tools.drive_tools import drive_read_doc_execute

    result = await drive_read_doc_execute({"file_id_or_url": "X"}, {})
    assert result["error"] is True


@pytest.mark.asyncio
async def test_drive_read_doc_missing_connector_returns_error():
    from app.mcp.tools.drive_tools import drive_read_doc_execute

    db = _mock_db(None)
    result = await drive_read_doc_execute(
        {"file_id_or_url": "X1234567890"},
        {"tenant_id": "ce3dfaad-626f-4992-84e9-500c8291ca0a", "db": db},
    )
    assert result["error"] is True
    msg = result["message"].lower()
    assert "sheets" in msg or "drive" in msg or "connector" in msg


@pytest.mark.asyncio
async def test_drive_read_doc_invalid_url_returns_error(monkeypatch):
    from app.mcp.tools import drive_tools

    connector = MagicMock()
    connector.encrypted_credentials = b"enc"
    connector.metadata_json = {}
    db = _mock_db(connector)
    monkeypatch.setattr(drive_tools, "decrypt_credentials", lambda _b: {"service_account_json": {}})

    result = await drive_tools.drive_read_doc_execute(
        {"file_id_or_url": "https://example.com/not-a-drive-url"},
        {"tenant_id": "ce3dfaad-626f-4992-84e9-500c8291ca0a", "db": db},
    )
    assert result["error"] is True


@pytest.mark.asyncio
async def test_drive_read_doc_parses_url_and_extracts(monkeypatch):
    from app.mcp.tools import drive_tools

    connector = MagicMock()
    connector.encrypted_credentials = b"enc"
    connector.metadata_json = {}
    db = _mock_db(connector)
    monkeypatch.setattr(
        drive_tools,
        "decrypt_credentials",
        lambda _b: {"service_account_json": {"client_email": "x@y"}},
    )
    monkeypatch.setattr(
        drive_tools.drive_client,
        "get_file_metadata",
        AsyncMock(
            return_value={
                "id": "FID",
                "name": "Doc",
                "mimeType": "application/vnd.google-apps.document",
                "webViewLink": "https://x",
            }
        ),
    )
    monkeypatch.setattr(
        drive_tools.extractors,
        "extract_by_mime",
        AsyncMock(return_value="hello from drive"),
    )

    result = await drive_tools.drive_read_doc_execute(
        {"file_id_or_url": "https://docs.google.com/document/d/FID1234567/edit"},
        {"tenant_id": "ce3dfaad-626f-4992-84e9-500c8291ca0a", "db": db},
    )
    assert result["error"] is False
    assert result["text"] == "hello from drive"
    assert result["source_name"] == "Doc"
    assert result["web_view_link"] == "https://x"
    assert result["truncated"] is False
    assert result["mime_type"] == "application/vnd.google-apps.document"


@pytest.mark.asyncio
async def test_drive_read_doc_truncates_long_text(monkeypatch):
    from app.mcp.tools import drive_tools

    connector = MagicMock()
    connector.encrypted_credentials = b"enc"
    connector.metadata_json = {}
    db = _mock_db(connector)
    monkeypatch.setattr(drive_tools, "decrypt_credentials", lambda _b: {"service_account_json": {}})
    monkeypatch.setattr(
        drive_tools.drive_client,
        "get_file_metadata",
        AsyncMock(
            return_value={
                "id": "FID",
                "name": "Doc",
                "mimeType": "text/plain",
                "webViewLink": "https://x",
            }
        ),
    )
    monkeypatch.setattr(
        drive_tools.extractors,
        "extract_by_mime",
        AsyncMock(return_value="x" * 60_000),
    )
    result = await drive_tools.drive_read_doc_execute(
        {"file_id_or_url": "FID1234567"},
        {"tenant_id": "ce3dfaad-626f-4992-84e9-500c8291ca0a", "db": db},
    )
    assert result["truncated"] is True
    assert len(result["text"]) == 50_000


def test_drive_read_doc_registered_in_registry():
    from app.mcp.registry import TOOL_REGISTRY

    assert "drive.read_doc" in TOOL_REGISTRY


def test_drive_read_doc_categorized_as_rag():
    from app.services.chat.tool_categories import categorize

    assert categorize("drive.read_doc") == "rag"
    assert categorize("drive_read_doc") == "rag"
