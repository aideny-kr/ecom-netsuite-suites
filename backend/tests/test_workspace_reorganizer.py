"""Tests for workspace_reorganizer — bulk reorganization of workspace files."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.workspace_reorganizer import reorganize_workspace


def _make_file(
    file_name: str,
    path: str,
    content: str = "",
    script_type: str | None = None,
    netsuite_file_id: str | None = None,
) -> MagicMock:
    """Create a mock WorkspaceFile."""
    f = MagicMock()
    f.id = uuid.uuid4()
    f.file_name = file_name
    f.path = path
    f.content = content
    f.script_type = script_type
    f.netsuite_file_id = netsuite_file_id
    f.is_directory = False
    return f


class TestReorganizeWorkspace:
    @pytest.mark.asyncio
    async def test_moves_files_by_content_detection(self):
        """Files with @NScriptType in content get moved to the correct folder."""
        ue_file = _make_file(
            "order_handler.js",
            "SuiteScripts/Uncategorized/order_handler.js",
            content="/** @NScriptType UserEventScript */ define([], () => {});",
        )

        mock_db = AsyncMock()
        # First query: get all non-directory files
        mock_result_files = MagicMock()
        mock_result_files.scalars.return_value.all.return_value = [ue_file]

        # Second query: get all file paths (for directory rebuild)
        mock_result_paths = MagicMock()
        mock_result_paths.all.return_value = [("SuiteScripts/User Event Scripts/order_handler.js",)]

        # Third query: get existing directories
        mock_result_dirs = MagicMock()
        mock_result_dirs.scalars.return_value.all.return_value = []

        # Fourth query: get tenant_id
        mock_result_tenant = MagicMock()
        mock_result_tenant.scalar_one.return_value = uuid.uuid4()

        mock_db.execute = AsyncMock(
            side_effect=[mock_result_files, mock_result_paths, mock_result_dirs, mock_result_tenant, mock_result_tenant]
        )

        ws_id = uuid.uuid4()
        result = await reorganize_workspace(mock_db, ws_id)

        assert result["moved"] == 1
        assert result["errors"] == 0
        assert ue_file.path == "SuiteScripts/User Event Scripts/order_handler.js"
        assert ue_file.script_type == "UserEventScript"

    @pytest.mark.asyncio
    async def test_skips_already_organized(self):
        """Files already in the correct folder are skipped."""
        ue_file = _make_file(
            "order_handler.js",
            "SuiteScripts/User Event Scripts/order_handler.js",
            content="/** @NScriptType UserEventScript */ define([], () => {});",
            script_type="UserEventScript",
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [ue_file]
        mock_db.execute = AsyncMock(return_value=mock_result)

        result = await reorganize_workspace(mock_db, uuid.uuid4())

        assert result["moved"] == 0
        assert result["skipped"] == 1

    @pytest.mark.asyncio
    async def test_skips_non_js_files(self):
        """Non-JS files are skipped."""
        txt_file = _make_file("readme.txt", "SuiteScripts/readme.txt")

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [txt_file]
        mock_db.execute = AsyncMock(return_value=mock_result)

        result = await reorganize_workspace(mock_db, uuid.uuid4())

        assert result["moved"] == 0
        assert result["skipped"] == 1

    @pytest.mark.asyncio
    async def test_filename_fallback_detection(self):
        """When content has no @NScriptType, filename heuristics are used."""
        rl_file = _make_file(
            "api_restlet.js",
            "SuiteScripts/misc/api_restlet.js",
            content="define(['N/record'], function(record) { });",
        )

        mock_db = AsyncMock()
        mock_result_files = MagicMock()
        mock_result_files.scalars.return_value.all.return_value = [rl_file]

        mock_result_paths = MagicMock()
        mock_result_paths.all.return_value = [("SuiteScripts/RESTlets/api_restlet.js",)]

        mock_result_dirs = MagicMock()
        mock_result_dirs.scalars.return_value.all.return_value = []

        mock_result_tenant = MagicMock()
        mock_result_tenant.scalar_one.return_value = uuid.uuid4()

        mock_db.execute = AsyncMock(
            side_effect=[mock_result_files, mock_result_paths, mock_result_dirs, mock_result_tenant]
        )

        result = await reorganize_workspace(mock_db, uuid.uuid4())

        assert result["moved"] == 1
        assert rl_file.path == "SuiteScripts/RESTlets/api_restlet.js"
        assert rl_file.script_type == "Restlet"

    @pytest.mark.asyncio
    async def test_preserves_netsuite_file_id(self):
        """netsuite_file_id is never changed during reorganization."""
        f = _make_file(
            "order_ue.js",
            "SuiteScripts/old_folder/order_ue.js",
            content="/** @NScriptType UserEventScript */",
            netsuite_file_id="12345",
        )

        mock_db = AsyncMock()
        mock_result_files = MagicMock()
        mock_result_files.scalars.return_value.all.return_value = [f]

        mock_result_paths = MagicMock()
        mock_result_paths.all.return_value = [("SuiteScripts/User Event Scripts/order_ue.js",)]

        mock_result_dirs = MagicMock()
        mock_result_dirs.scalars.return_value.all.return_value = []

        mock_result_tenant = MagicMock()
        mock_result_tenant.scalar_one.return_value = uuid.uuid4()

        mock_db.execute = AsyncMock(
            side_effect=[mock_result_files, mock_result_paths, mock_result_dirs, mock_result_tenant]
        )

        await reorganize_workspace(mock_db, uuid.uuid4())

        # netsuite_file_id should not have been touched
        assert f.netsuite_file_id == "12345"

    @pytest.mark.asyncio
    async def test_idempotent(self):
        """Running reorganize twice produces the same result."""
        f = _make_file(
            "handler.js",
            "SuiteScripts/User Event Scripts/handler.js",
            content="/** @NScriptType UserEventScript */",
            script_type="UserEventScript",
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [f]
        mock_db.execute = AsyncMock(return_value=mock_result)

        r1 = await reorganize_workspace(mock_db, uuid.uuid4())
        r2 = await reorganize_workspace(mock_db, uuid.uuid4())

        assert r1 == r2 == {"moved": 0, "skipped": 1, "errors": 0}
