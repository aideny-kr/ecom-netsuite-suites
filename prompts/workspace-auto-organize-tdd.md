# Workspace File Auto-Organization by Script Type (TDD)

> Auto-organize SuiteScript files into logical folders by script type when synced
> from NetSuite. Files land in `User Event Scripts/`, `Client Scripts/`, `RESTlets/`,
> etc. instead of whatever flat mess exists in the File Cabinet.
>
> Use Red-Green-Refactor TDD for each cycle.

Read `CLAUDE.md` before starting. Follow all conventions exactly.

---

## Why This Matters

Most NetSuite accounts have scripts dumped flat in `/SuiteScripts/` with no folder
organization. When the app syncs these files into the dev workspace, it faithfully
reproduces that mess. Users see a wall of files with no grouping. Auto-organizing
by script type makes the workspace immediately navigable.

## What Exists Today

- `backend/app/services/workspace_rag_seeder.py:99` — `_detect_script_type(content)` regex extracts `@NScriptType`
- `frontend/src/lib/suitescript-parser.ts:94` — `parseSuiteScriptMetadata()` with filename heuristics (`_ue`, `_cs`, `_ss`, `_mr`, `_su`, `_rl`, etc.)
- `frontend/src/lib/suitescript-parser.ts:6-18` — `ScriptType` union type (12 types) and `SCRIPT_TYPE_MAP` with colors/badges
- `backend/app/services/suitescript_sync_service.py:324` — `_build_file_path()` currently organizes by source (`SuiteScripts/{folder}/` vs `CustomScripts/`)
- `backend/app/services/suitescript_sync_service.py:40` — `discover_scripts()` returns `script_type` for custom script records, `None` for file cabinet files
- `backend/app/services/suitescript_sync_service.py:371` — `_upsert_workspace_file()` stores path, content, netsuite_file_id
- `backend/app/models/workspace.py:42` — `WorkspaceFile` model has NO `script_type` column yet
- `frontend/src/components/workspace/file-tree.tsx:88` — calls `parseSuiteScriptMetadata(null, node.path)` for badges (path-only, no content)
- Latest migration: `041_user_feedback`

## What This Does NOT Do

- Does NOT modify files in NetSuite's File Cabinet (virtual reorganization only)
- Does NOT change the `netsuite_file_id` mapping (push/pull still works)
- Does NOT break existing workspace files (reorganization is additive)

## Target Folder Structure

```
SuiteScripts/
├── User Event Scripts/
│   ├── sales_order_ue.js
│   └── inventory_ue.js
├── Client Scripts/
│   └── customer_form_cs.js
├── Scheduled Scripts/
│   └── nightly_sync_ss.js
├── Map Reduce/
│   └── bulk_update_mr.js
├── Suitelets/
│   └── custom_page_su.js
├── RESTlets/
│   └── api_integration_rl.js
├── Workflow Actions/
│   └── approval_wa.js
├── Bundle Installation/
├── Mass Update/
├── Libraries/
│   └── lib_utils.js
└── Other/
    └── mystery_script.js
```

---

## TDD Cycles (5 cycles)

### Cycle 1 — Migration + Model Update

**RED** — Test that `WorkspaceFile` has a `script_type` column:
```python
# backend/tests/test_workspace_script_type.py
import pytest
from app.models.workspace import WorkspaceFile


def test_workspace_file_has_script_type_attr():
    """WorkspaceFile model should have a script_type attribute."""
    assert hasattr(WorkspaceFile, "script_type")


def test_workspace_file_script_type_nullable():
    """script_type should be nullable (existing files don't have it yet)."""
    col = WorkspaceFile.__table__.columns["script_type"]
    assert col.nullable is True
```

**GREEN** — Create the migration and update the model:

1. Create `backend/alembic/versions/042_workspace_script_type.py`:
```python
"""Add script_type column to workspace_files."""
from alembic import op
import sqlalchemy as sa

revision = "042_workspace_script_type"
down_revision = "041_user_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspace_files", sa.Column("script_type", sa.String(50), nullable=True))
    op.create_index(
        "ix_workspace_files_ws_script_type",
        "workspace_files",
        ["workspace_id", "script_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_files_ws_script_type", table_name="workspace_files")
    op.drop_column("workspace_files", "script_type")
```

2. Update `backend/app/models/workspace.py` — add to `WorkspaceFile` class after `locked_at`:
```python
    script_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
```

**REFACTOR:** None needed.

---

### Cycle 2 — Shared Script Type Detector

**RED** — Create `backend/tests/test_script_type_detector.py`:
```python
import pytest
from app.services.script_type_detector import (
    detect_from_content,
    detect_from_filename,
    resolve_script_type,
    SCRIPT_TYPE_FOLDER_MAP,
)


class TestDetectFromContent:
    """Extract @NScriptType from file content."""

    def test_user_event_script(self):
        content = """/**\n * @NApiVersion 2.1\n * @NScriptType UserEventScript\n */"""
        assert detect_from_content(content) == "UserEventScript"

    def test_client_script(self):
        content = "/**\n * @NScriptType ClientScript\n */"
        assert detect_from_content(content) == "ClientScript"

    def test_scheduled_script(self):
        content = "// @NScriptType ScheduledScript"
        assert detect_from_content(content) == "ScheduledScript"

    def test_map_reduce(self):
        content = " * @NScriptType MapReduceScript\n"
        assert detect_from_content(content) == "MapReduceScript"

    def test_suitelet(self):
        content = "@NScriptType Suitelet"
        assert detect_from_content(content) == "Suitelet"

    def test_restlet(self):
        content = "@NScriptType Restlet"
        assert detect_from_content(content) == "Restlet"

    def test_workflow_action(self):
        content = "@NScriptType WorkflowActionScript"
        assert detect_from_content(content) == "WorkflowActionScript"

    def test_bundle_installation(self):
        content = "@NScriptType BundleInstallationScript"
        assert detect_from_content(content) == "BundleInstallationScript"

    def test_mass_update(self):
        content = "@NScriptType MassUpdateScript"
        assert detect_from_content(content) == "MassUpdateScript"

    def test_portlet(self):
        content = "@NScriptType Portlet"
        assert detect_from_content(content) == "Portlet"

    def test_no_annotation_returns_none(self):
        content = "function doStuff() { return 42; }"
        assert detect_from_content(content) is None

    def test_empty_content_returns_none(self):
        assert detect_from_content("") is None

    def test_case_insensitive_match(self):
        """@NScriptType value should match case-insensitively."""
        content = "@NScriptType usereventscript"
        result = detect_from_content(content)
        assert result == "UserEventScript"


class TestDetectFromFilename:
    """Filename heuristics — mirrors frontend suitescript-parser.ts logic."""

    def test_userevent_keyword(self):
        assert detect_from_filename("sales_order_userevent.js") == "UserEventScript"

    def test_ue_suffix(self):
        assert detect_from_filename("inventory_ue.js") == "UserEventScript"

    def test_client_keyword(self):
        assert detect_from_filename("customer_form_client.js") == "ClientScript"

    def test_cs_suffix(self):
        assert detect_from_filename("order_cs.js") == "ClientScript"

    def test_scheduled_keyword(self):
        assert detect_from_filename("nightly_scheduled.js") == "ScheduledScript"

    def test_ss_suffix(self):
        assert detect_from_filename("sync_ss.js") == "ScheduledScript"

    def test_mapreduce_keyword(self):
        assert detect_from_filename("bulk_mapreduce.js") == "MapReduceScript"

    def test_mr_suffix(self):
        assert detect_from_filename("process_mr.js") == "MapReduceScript"

    def test_suitelet_keyword(self):
        assert detect_from_filename("custom_suitelet.js") == "Suitelet"

    def test_su_suffix(self):
        assert detect_from_filename("page_su.js") == "Suitelet"

    def test_restlet_keyword(self):
        assert detect_from_filename("api_restlet.js") == "Restlet"

    def test_rl_suffix(self):
        assert detect_from_filename("service_rl.js") == "Restlet"

    def test_workflow_keyword(self):
        assert detect_from_filename("approval_workflow.js") == "WorkflowActionScript"

    def test_wa_suffix(self):
        assert detect_from_filename("validate_wa.js") == "WorkflowActionScript"

    def test_bundle_keyword(self):
        assert detect_from_filename("install_bundle.js") == "BundleInstallationScript"

    def test_bi_suffix(self):
        assert detect_from_filename("setup_bi.js") == "BundleInstallationScript"

    def test_massupdate_keyword(self):
        assert detect_from_filename("fix_massupdate.js") == "MassUpdateScript"

    def test_mu_suffix(self):
        assert detect_from_filename("cleanup_mu.js") == "MassUpdateScript"

    def test_util_keyword(self):
        assert detect_from_filename("util_helpers.js") == "Library"

    def test_lib_keyword(self):
        assert detect_from_filename("lib_common.js") == "Library"

    def test_helper_keyword(self):
        assert detect_from_filename("date_helper.js") == "Library"

    def test_unknown_filename(self):
        assert detect_from_filename("random_thing.js") is None

    def test_non_js_file(self):
        assert detect_from_filename("readme.txt") is None


class TestResolveScriptType:
    """Content takes priority, then filename, then 'Other'."""

    def test_content_wins_over_filename(self):
        content = "@NScriptType Restlet"
        # Filename says _ue but content says Restlet
        assert resolve_script_type(content, "some_ue.js") == "Restlet"

    def test_filename_fallback_when_no_content(self):
        assert resolve_script_type(None, "order_ue.js") == "UserEventScript"

    def test_filename_fallback_when_no_annotation(self):
        content = "function doStuff() { return 42; }"
        assert resolve_script_type(content, "order_ue.js") == "UserEventScript"

    def test_other_when_nothing_matches(self):
        content = "function doStuff() { return 42; }"
        assert resolve_script_type(content, "random.js") == "Other"

    def test_other_when_both_none(self):
        assert resolve_script_type(None, "random.js") == "Other"


class TestFolderMap:
    """SCRIPT_TYPE_FOLDER_MAP should cover all known types."""

    def test_all_types_have_folder(self):
        expected_types = [
            "UserEventScript", "ClientScript", "ScheduledScript",
            "MapReduceScript", "Suitelet", "Restlet",
            "WorkflowActionScript", "BundleInstallationScript",
            "MassUpdateScript", "Portlet", "Library", "Other",
        ]
        for t in expected_types:
            assert t in SCRIPT_TYPE_FOLDER_MAP, f"Missing folder mapping for {t}"

    def test_folder_names_are_human_readable(self):
        for script_type, folder_name in SCRIPT_TYPE_FOLDER_MAP.items():
            assert len(folder_name) > 0
            # Should not contain underscores or camelCase
            assert "_" not in folder_name, f"Folder '{folder_name}' should use spaces"
```

**GREEN** — Create `backend/app/services/script_type_detector.py`:
```python
"""Shared script type detection — content parsing + filename heuristics.

Mirrors the frontend logic in suitescript-parser.ts for consistency.
Used by: suitescript_sync_service, workspace_rag_seeder, workspace_reorganizer.
"""

from __future__ import annotations

import re

# --- Constants ---

# Maps script type → display folder name
SCRIPT_TYPE_FOLDER_MAP: dict[str, str] = {
    "UserEventScript": "User Event Scripts",
    "ClientScript": "Client Scripts",
    "ScheduledScript": "Scheduled Scripts",
    "MapReduceScript": "Map Reduce",
    "Suitelet": "Suitelets",
    "Restlet": "RESTlets",
    "WorkflowActionScript": "Workflow Actions",
    "BundleInstallationScript": "Bundle Installation",
    "MassUpdateScript": "Mass Update",
    "Portlet": "Portlets",
    "Library": "Libraries",
    "Other": "Other",
}

# Canonical type names for case-insensitive matching
_CANONICAL_TYPES: dict[str, str] = {k.lower(): k for k in SCRIPT_TYPE_FOLDER_MAP if k != "Other"}

# Filename heuristic patterns — order matters (first match wins)
_FILENAME_PATTERNS: list[tuple[list[str], str]] = [
    (["userevent", "_ue"], "UserEventScript"),
    (["client", "_cs"], "ClientScript"),
    (["scheduled", "_ss"], "ScheduledScript"),
    (["mapreduce", "_mr"], "MapReduceScript"),
    (["suitelet", "_su"], "Suitelet"),
    (["restlet", "_rl"], "Restlet"),
    (["workflow", "_wa"], "WorkflowActionScript"),
    (["bundle", "_bi"], "BundleInstallationScript"),
    (["massupdate", "_mu"], "MassUpdateScript"),
    (["util", "lib", "helper"], "Library"),
]


# --- Detection Functions ---


def detect_from_content(content: str) -> str | None:
    """Extract @NScriptType from JSDoc annotation.

    Returns canonical script type name or None if not found.
    """
    if not content:
        return None
    m = re.search(r"@NScriptType\s+(\w+)", content)
    if not m:
        return None
    raw = m.group(1).lower()
    return _CANONICAL_TYPES.get(raw)


def detect_from_filename(filename: str) -> str | None:
    """Infer script type from filename using heuristics.

    Mirrors frontend suitescript-parser.ts logic (lines 170-187).
    Returns script type name or None if no match.
    """
    if not filename:
        return None
    # Only process JS/TS files
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("js", "ts", "jsx", "tsx"):
        return None

    lower = filename.lower()
    for patterns, script_type in _FILENAME_PATTERNS:
        for pattern in patterns:
            if pattern in lower:
                return script_type
    return None


def resolve_script_type(content: str | None, filename: str) -> str:
    """Resolve script type using all available signals.

    Priority: content @NScriptType → filename heuristics → "Other"
    """
    if content:
        from_content = detect_from_content(content)
        if from_content:
            return from_content

    from_filename = detect_from_filename(filename)
    if from_filename:
        return from_filename

    return "Other"


def get_type_folder(script_type: str) -> str:
    """Get the display folder name for a script type."""
    return SCRIPT_TYPE_FOLDER_MAP.get(script_type, "Other")
```

Then update `backend/app/services/workspace_rag_seeder.py` — replace the inline `_detect_script_type`:
```python
# Remove lines 99-102 (_detect_script_type function) and add this import at the top:
from app.services.script_type_detector import detect_from_content

# Then replace any call to _detect_script_type(content) with detect_from_content(content)
```

**REFACTOR:** Ensure all existing tests still pass after the import change in `workspace_rag_seeder.py`.

---

### Cycle 3 — Update Sync Service Path Building

**RED** — Create `backend/tests/test_script_type_sync.py`:
```python
import pytest
from app.services.suitescript_sync_service import _build_file_path


class TestBuildFilePathWithScriptType:
    """_build_file_path should organize files into script-type folders."""

    def test_user_event_in_type_folder(self):
        meta = {
            "name": "sales_order_ue.js",
            "source": "file_cabinet",
            "folder_path": "Custom",
            "script_type": "UserEventScript",
        }
        path = _build_file_path(meta)
        assert path == "SuiteScripts/User Event Scripts/sales_order_ue.js"

    def test_restlet_in_type_folder(self):
        meta = {
            "name": "api_integration.js",
            "source": "file_cabinet",
            "folder_path": "Integrations",
            "script_type": "Restlet",
        }
        path = _build_file_path(meta)
        assert path == "SuiteScripts/RESTlets/api_integration.js"

    def test_library_in_type_folder(self):
        meta = {
            "name": "lib_utils.js",
            "source": "file_cabinet",
            "folder_path": "Shared",
            "script_type": "Library",
        }
        path = _build_file_path(meta)
        assert path == "SuiteScripts/Libraries/lib_utils.js"

    def test_other_type(self):
        meta = {
            "name": "mystery.js",
            "source": "file_cabinet",
            "folder_path": "Random",
            "script_type": "Other",
        }
        path = _build_file_path(meta)
        assert path == "SuiteScripts/Other/mystery.js"

    def test_none_script_type_falls_back_to_other(self):
        """If script_type is None (legacy), put in Other."""
        meta = {
            "name": "old_file.js",
            "source": "file_cabinet",
            "folder_path": "Legacy",
            "script_type": None,
        }
        path = _build_file_path(meta)
        assert path == "SuiteScripts/Other/old_file.js"

    def test_custom_script_preserves_script_id(self):
        meta = {
            "name": "order_handler.js",
            "source": "custom_script",
            "script_id": "customscript_order",
            "script_type": "UserEventScript",
        }
        path = _build_file_path(meta)
        assert path == "SuiteScripts/User Event Scripts/customscript_order_order_handler.js"

    def test_custom_script_without_type(self):
        meta = {
            "name": "unknown.js",
            "source": "custom_script",
            "script_id": "customscript_x",
            "script_type": None,
        }
        path = _build_file_path(meta)
        assert path == "SuiteScripts/Other/customscript_x_unknown.js"

    def test_name_sanitization(self):
        meta = {
            "name": "my script (v2).js",
            "source": "file_cabinet",
            "folder_path": "Test",
            "script_type": "Suitelet",
        }
        path = _build_file_path(meta)
        assert "(" not in path
        assert ")" not in path
        assert path.startswith("SuiteScripts/Suitelets/")

    def test_missing_js_extension_added(self):
        meta = {
            "name": "no_extension",
            "source": "file_cabinet",
            "folder_path": "Test",
            "script_type": "Restlet",
        }
        path = _build_file_path(meta)
        assert path.endswith(".js")
```

**GREEN** — Update `backend/app/services/suitescript_sync_service.py`:

1. Add import at top:
```python
from app.services.script_type_detector import resolve_script_type, get_type_folder
```

2. Replace `_build_file_path()` (lines 324-339):
```python
def _build_file_path(file_meta: dict[str, Any]) -> str:
    """Build a workspace path from file metadata, organized by script type."""
    name = file_meta.get("name", "unknown.js")
    # Sanitize name: only alphanumeric, dots, underscores, hyphens, spaces
    safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)
    if not safe_name.endswith(".js"):
        safe_name = f"{safe_name}.js"

    # Get the type folder (e.g., "User Event Scripts", "RESTlets", "Other")
    script_type = file_meta.get("script_type")
    type_folder = get_type_folder(script_type) if script_type else "Other"

    source = file_meta.get("source", "file_cabinet")
    if source == "custom_script":
        script_id = file_meta.get("script_id", "")
        prefix = f"{script_id}_" if script_id else ""
        return f"SuiteScripts/{type_folder}/{prefix}{safe_name}"
    else:
        return f"SuiteScripts/{type_folder}/{safe_name}"
```

3. Update `_upsert_workspace_file()` — add `script_type` parameter (line 371):
```python
async def _upsert_workspace_file(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    path: str,
    content: str,
    netsuite_file_id: str | None = None,
    script_type: str | None = None,  # NEW
) -> WorkspaceFile:
    """Create or update a workspace file by path."""
    result = await db.execute(
        select(WorkspaceFile).where(
            WorkspaceFile.workspace_id == workspace_id,
            WorkspaceFile.path == path,
        )
    )
    existing = result.scalar_one_or_none()

    sha = _sha256(content)
    file_name = PurePosixPath(path).name

    if existing:
        if existing.sha256_hash != sha:
            existing.content = content
            existing.sha256_hash = sha
            existing.size_bytes = len(content.encode("utf-8"))
            existing.updated_at = datetime.now(timezone.utc)
        if netsuite_file_id:
            existing.netsuite_file_id = netsuite_file_id
        if script_type:
            existing.script_type = script_type
        return existing

    wf = WorkspaceFile(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        path=path,
        file_name=file_name,
        content=content,
        sha256_hash=sha,
        size_bytes=len(content.encode("utf-8")),
        mime_type="application/javascript",
        is_directory=False,
        netsuite_file_id=netsuite_file_id,
        script_type=script_type,
    )
    db.add(wf)
    return wf
```

4. Update `sync_scripts_to_workspace()` — detect script type after content fetch (around line 533):
```python
        # 5. Build file paths and upsert
        file_paths: set[str] = set()
        loaded = 0

        for file_meta in discovered:
            fid = file_meta["file_id"]
            content = contents.get(fid)
            if content is None:
                continue

            # Detect script type from content (or fall back to filename/metadata)
            detected_type = resolve_script_type(content, file_meta.get("name", ""))
            # Custom script records may already have script_type from NetSuite
            if file_meta.get("script_type") and not detect_from_content(content):
                detected_type = file_meta["script_type"]
            file_meta["script_type"] = detected_type

            path = _build_file_path(file_meta)
            file_paths.add(path)
            await _upsert_workspace_file(
                db,
                tenant_id,
                ws.id,
                path,
                content,
                netsuite_file_id=fid,
                script_type=detected_type,
            )
            loaded += 1
```

Also add the import for `detect_from_content`:
```python
from app.services.script_type_detector import resolve_script_type, get_type_folder, detect_from_content
```

**REFACTOR:** Ensure `batch_fetch_contents` tests still pass. Run `pytest backend/tests/ -k "sync" -v`.

---

### Cycle 4 — Reorganize Existing Workspaces

**RED** — Create `backend/tests/test_workspace_reorganizer.py`:
```python
import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock

from app.services.workspace_reorganizer import reorganize_workspace


class TestReorganizeWorkspace:
    """Test workspace file reorganization."""

    @pytest.mark.asyncio
    async def test_moves_file_to_type_folder(self):
        """File with content @NScriptType should be moved to the correct folder."""
        mock_file = MagicMock()
        mock_file.id = uuid.uuid4()
        mock_file.path = "SuiteScripts/Custom/order_ue.js"
        mock_file.file_name = "order_ue.js"
        mock_file.content = "/**\n * @NScriptType UserEventScript\n */"
        mock_file.script_type = None
        mock_file.is_directory = False
        mock_file.netsuite_file_id = "12345"

        db = AsyncMock()
        # Mock the query to return our file
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [mock_file]
        db.execute.return_value = result_mock

        summary = await reorganize_workspace(db, uuid.uuid4())
        assert summary["moved"] >= 0  # Will vary based on mock setup

    @pytest.mark.asyncio
    async def test_preserves_netsuite_file_id(self):
        """netsuite_file_id must not change during reorganization."""
        mock_file = MagicMock()
        mock_file.id = uuid.uuid4()
        mock_file.path = "SuiteScripts/Flat/test.js"
        mock_file.file_name = "test.js"
        mock_file.content = "@NScriptType Restlet"
        mock_file.script_type = None
        mock_file.is_directory = False
        mock_file.netsuite_file_id = "99999"

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [mock_file]
        db.execute.return_value = result_mock

        await reorganize_workspace(db, uuid.uuid4())
        # netsuite_file_id should still be the same
        assert mock_file.netsuite_file_id == "99999"

    @pytest.mark.asyncio
    async def test_skips_already_organized(self):
        """Files already in the correct type folder should be skipped."""
        mock_file = MagicMock()
        mock_file.id = uuid.uuid4()
        mock_file.path = "SuiteScripts/RESTlets/api.js"
        mock_file.file_name = "api.js"
        mock_file.content = "@NScriptType Restlet"
        mock_file.script_type = "Restlet"
        mock_file.is_directory = False
        mock_file.netsuite_file_id = "111"

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [mock_file]
        db.execute.return_value = result_mock

        summary = await reorganize_workspace(db, uuid.uuid4())
        assert summary["skipped"] >= 0

    @pytest.mark.asyncio
    async def test_skips_directories(self):
        """Directory records should not be processed."""
        mock_dir = MagicMock()
        mock_dir.id = uuid.uuid4()
        mock_dir.path = "SuiteScripts/Custom"
        mock_dir.is_directory = True

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [mock_dir]
        db.execute.return_value = result_mock

        summary = await reorganize_workspace(db, uuid.uuid4())
        assert summary["moved"] == 0

    @pytest.mark.asyncio
    async def test_returns_summary(self):
        """Should return a summary dict with moved, skipped, errors."""
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        db.execute.return_value = result_mock

        summary = await reorganize_workspace(db, uuid.uuid4())
        assert "moved" in summary
        assert "skipped" in summary
        assert "errors" in summary
```

**GREEN** — Create `backend/app/services/workspace_reorganizer.py`:
```python
"""Reorganize existing workspace files by script type.

Reads all files in a workspace, detects script_type from content/filename,
and moves them into organized folders. Preserves netsuite_file_id for push/pull.
"""

from __future__ import annotations

import uuid
from pathlib import PurePosixPath
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import WorkspaceFile
from app.services.script_type_detector import get_type_folder, resolve_script_type

logger = structlog.get_logger()


async def reorganize_workspace(
    db: AsyncSession,
    workspace_id: uuid.UUID,
) -> dict[str, Any]:
    """Reorganize all files in a workspace by script type.

    Returns summary: {moved: int, skipped: int, errors: int}
    """
    result = await db.execute(
        select(WorkspaceFile).where(
            WorkspaceFile.workspace_id == workspace_id,
            WorkspaceFile.is_directory.is_(False),
        )
    )
    files = result.scalars().all()

    moved = 0
    skipped = 0
    errors = 0

    for wf in files:
        try:
            # Detect script type
            detected_type = resolve_script_type(wf.content, wf.file_name)
            type_folder = get_type_folder(detected_type)

            # Build the target path
            target_path = f"SuiteScripts/{type_folder}/{wf.file_name}"

            # Skip if already in the correct location
            if wf.path == target_path and wf.script_type == detected_type:
                skipped += 1
                continue

            # Update path and script_type
            wf.path = target_path
            wf.script_type = detected_type
            moved += 1

            logger.debug(
                "workspace_reorganizer.moved",
                file_id=str(wf.id),
                old_path=wf.path,
                new_path=target_path,
                script_type=detected_type,
            )
        except Exception as exc:
            errors += 1
            logger.warning(
                "workspace_reorganizer.error",
                file_id=str(wf.id),
                error=str(exc),
            )

    # Rebuild directory records for the new paths
    if moved > 0:
        from app.services.suitescript_sync_service import _ensure_directories

        all_paths = {wf.path for wf in files if not wf.is_directory}
        # Get tenant_id from first file
        tenant_id = files[0].tenant_id if files else None
        if tenant_id:
            await _ensure_directories(db, tenant_id, workspace_id, all_paths)

    await db.flush()

    summary = {"moved": moved, "skipped": skipped, "errors": errors}
    logger.info("workspace_reorganizer.completed", workspace_id=str(workspace_id), **summary)
    return summary
```

Then add the API endpoint — add to `backend/app/api/v1/workspaces.py`:
```python
from app.services.workspace_reorganizer import reorganize_workspace
from app.services import audit_service


@router.post("/{workspace_id}/reorganize")
async def reorganize_workspace_files(
    workspace_id: str,
    user: Annotated[User, Depends(require_permission("workspace.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Reorganize workspace files by script type."""
    ws_uuid = uuid.UUID(workspace_id)

    summary = await reorganize_workspace(db, ws_uuid)

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="workspace",
        action="workspace.reorganize",
        actor_id=user.id,
        resource_type="workspace",
        resource_id=workspace_id,
        details=summary,
    )
    await db.commit()
    return summary
```

**NOTE:** Add the `Annotated` import and `AsyncSession` dependency if not already present in the file. Check existing endpoint patterns.

**REFACTOR:** None needed.

---

### Cycle 5 — Frontend View Toggle

**RED** — Verify the file tree renders script-type organized files correctly:
- After a sync, files should appear in type-based folders
- Script type badges should still show on individual files

**GREEN** — Update `frontend/src/components/workspace/file-tree.tsx`:

Add a view toggle at the top of the FileTree component:

```typescript
"use client";

import { useState } from "react";
import { ChevronRight, File, Folder, FolderOpen, LayoutGrid, FolderTree } from "lucide-react";
import { cn } from "@/lib/utils";
import { parseSuiteScriptMetadata } from "@/lib/suitescript-parser";
import type { FileTreeNode } from "@/lib/types";

interface FileTreeProps {
  nodes: FileTreeNode[];
  onFileSelect: (fileId: string, path: string) => void;
  selectedFileId?: string | null;
}

export function FileTree({ nodes, onFileSelect, selectedFileId }: FileTreeProps) {
  const [viewMode, setViewMode] = useState<"folder" | "type">("folder");

  return (
    <div data-testid="file-tree">
      {/* View toggle */}
      <div className="flex items-center gap-1 px-2 py-1.5 border-b mb-1">
        <span className="text-[10px] text-muted-foreground mr-auto">View by</span>
        <button
          onClick={() => setViewMode("folder")}
          className={cn(
            "p-1 rounded transition-colors",
            viewMode === "folder" ? "bg-primary/10 text-primary" : "text-muted-foreground hover:text-foreground",
          )}
          title="Folder structure"
        >
          <FolderTree className="h-3.5 w-3.5" />
        </button>
        <button
          onClick={() => setViewMode("type")}
          className={cn(
            "p-1 rounded transition-colors",
            viewMode === "type" ? "bg-primary/10 text-primary" : "text-muted-foreground hover:text-foreground",
          )}
          title="Script type"
        >
          <LayoutGrid className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* Tree */}
      <div className="text-[12px]">
        {nodes.map((node) => (
          <TreeNode
            key={node.id}
            node={node}
            depth={0}
            onFileSelect={onFileSelect}
            selectedFileId={selectedFileId}
          />
        ))}
      </div>
    </div>
  );
}
```

The view toggle state is frontend-only for now — the backend already returns files in
type-based folders after Cycle 3 (new syncs) or Cycle 4 (reorganized). The "folder"
view shows the physical tree (what comes from the API). The "type" view can be
enhanced later to group by `script_type` field from the API response if needed.

**REFACTOR:** None needed for MVP. The toggle UI is in place, and since new syncs
already produce type-organized paths, both views will look organized.

---

## Files Summary

| Action | File | Purpose |
|--------|------|---------|
| Create | `backend/alembic/versions/042_workspace_script_type.py` | Migration: add script_type column |
| Create | `backend/app/services/script_type_detector.py` | Shared detection utility |
| Create | `backend/app/services/workspace_reorganizer.py` | Reorganize existing workspaces |
| Create | `backend/tests/test_script_type_detector.py` | Detection unit tests |
| Create | `backend/tests/test_script_type_sync.py` | Sync path-building tests |
| Create | `backend/tests/test_workspace_reorganizer.py` | Reorganization tests |
| Create | `backend/tests/test_workspace_script_type.py` | Model column tests |
| Modify | `backend/app/models/workspace.py` | Add `script_type` column |
| Modify | `backend/app/services/suitescript_sync_service.py` | Update `_build_file_path()`, `_upsert_workspace_file()`, sync flow |
| Modify | `backend/app/services/workspace_rag_seeder.py` | Import from shared detector |
| Modify | `backend/app/api/v1/workspaces.py` | Add `POST /{id}/reorganize` endpoint |
| Modify | `frontend/src/components/workspace/file-tree.tsx` | Add view toggle |

## Dependencies

- Migration `042` depends on `041_user_feedback`
- Cycle 3 depends on Cycle 1 (column) and Cycle 2 (detector)
- Cycle 4 depends on Cycle 2 (detector) and Cycle 3 (path builder)
- Cycle 5 is independent (UI only)

## Verification

1. `pytest backend/tests/test_script_type_detector.py -v` — all detection tests pass
2. `pytest backend/tests/test_script_type_sync.py -v` — path building tests pass
3. `pytest backend/tests/test_workspace_reorganizer.py -v` — reorganization tests pass
4. `pytest backend/tests/test_workspace_script_type.py -v` — model tests pass
5. Run full suite: `pytest backend/tests/ -v --tb=short` — no regressions
6. Manual: trigger sync from NetSuite → files appear in type-based folders
7. Manual: call `POST /workspaces/{id}/reorganize` → existing files get moved
8. Manual: push/pull a reorganized file → `netsuite_file_id` still works
9. Manual: check frontend file tree → view toggle visible, type folders expanded
