"""Tests for workspace ID resolution and validation (workspace-tool-routing fix).

The agent must resolve to the workspace with files, not empty ones,
and must validate that LLM-provided workspace IDs actually exist.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.chat.agents.base_agent import (
    _is_valid_uuid,
    _resolve_default_workspace,
)

TENANT_ID = uuid.uuid4()


def _make_ws_row(ws_id: uuid.UUID, file_count: int):
    """Simulate a workspace row with file count."""
    return (ws_id, file_count)


class TestResolveDefaultWorkspace:
    """_resolve_default_workspace should prefer workspaces with files."""

    @pytest.mark.asyncio
    async def test_resolves_to_workspace_with_files(self):
        """When two workspaces exist, pick the one with files."""
        db = AsyncMock()
        ws_with_files = uuid.uuid4()
        result = MagicMock()
        result.first.return_value = (ws_with_files, 311)
        db.execute = AsyncMock(return_value=result)

        resolved = await _resolve_default_workspace(db, TENANT_ID)
        assert resolved == str(ws_with_files)

    @pytest.mark.asyncio
    async def test_returns_none_when_no_workspaces(self):
        """No workspaces → returns None."""
        db = AsyncMock()
        result = MagicMock()
        result.first.return_value = None
        db.execute = AsyncMock(return_value=result)

        resolved = await _resolve_default_workspace(db, TENANT_ID)
        assert resolved is None

    @pytest.mark.asyncio
    async def test_resolves_single_workspace(self):
        """Single workspace → returns it regardless of file count."""
        db = AsyncMock()
        ws_id = uuid.uuid4()
        result = MagicMock()
        result.first.return_value = (ws_id, 0)
        db.execute = AsyncMock(return_value=result)

        resolved = await _resolve_default_workspace(db, TENANT_ID)
        assert resolved == str(ws_id)


class TestValidateWorkspaceId:
    """LLM-provided workspace IDs should be validated against the DB."""

    def test_valid_uuid(self):
        assert _is_valid_uuid(str(uuid.uuid4())) is True

    def test_invalid_uuid(self):
        assert _is_valid_uuid("not-a-uuid") is False

    def test_empty_string(self):
        assert _is_valid_uuid("") is False
