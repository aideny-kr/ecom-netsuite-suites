"""SuiteScript sync service — discover and load scripts from NetSuite into workspace.

Workflow:
  1. SuiteQL queries discover JavaScript files + custom script records
  2. REST API fetches file content (base64) in batches
  3. Files upserted into a dedicated 'NetSuite Scripts' workspace
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.script_sync import ScriptSyncState
from app.models.workspace import Workspace, WorkspaceFile
from app.services.netsuite_client import _normalize_account_id, execute_suiteql

logger = structlog.get_logger()

# --- Constants ---

WORKSPACE_NAME = "NetSuite Scripts"
MAX_FILE_SIZE = 256 * 1024  # 256 KB — matches workspace_service limit
BATCH_SIZE = 50
BATCH_DELAY_SECONDS = 0.2  # 200ms between batches
FETCH_TIMEOUT = 15  # seconds per file fetch


# --- Discovery ---


async def discover_scripts(access_token: str, account_id: str) -> list[dict[str, Any]]:
    """Discover JavaScript files and custom scripts via SuiteQL.

    Returns a combined list of file metadata dicts with keys:
      file_id, name, folder, script_type, size, modified, source
    """
    files: list[dict[str, Any]] = []

    # Query 1: JavaScript files in the File Cabinet
    try:
        result = await execute_suiteql(
            access_token=access_token,
            account_id=account_id,
            query=(
                "SELECT id, name, folder, filetype, filesize, lastmodifieddate "
                "FROM file "
                "WHERE filetype = 'JAVASCRIPT' "
                "AND isinactive = 'F' "
                "ORDER BY lastmodifieddate DESC"
            ),
            limit=1000,
        )
        for row in result.get("rows", []):
            cols = result.get("columns", [])
            item = dict(zip(cols, row)) if cols else {}
            files.append(
                {
                    "file_id": str(item.get("id", "")),
                    "name": item.get("name", "unknown.js"),
                    "folder": str(item.get("folder", "")),
                    "script_type": None,
                    "size": int(item.get("filesize", 0) or 0),
                    "modified": item.get("lastmodifieddate"),
                    "source": "file_cabinet",
                }
            )
        logger.info("suitescript_sync.files_discovered", count=len(files))
    except Exception:
        logger.warning("suitescript_sync.file_discovery_failed", exc_info=True)

    # Query 2: Custom script records (active only)
    scripts: list[dict[str, Any]] = []
    try:
        result = await execute_suiteql(
            access_token=access_token,
            account_id=account_id,
            query=(
                "SELECT id, scriptid, name, scripttype, scriptfile "
                "FROM customscript "
                "WHERE isinactive = 'F' "
                "ORDER BY name ASC"
            ),
            limit=1000,
        )
        for row in result.get("rows", []):
            cols = result.get("columns", [])
            item = dict(zip(cols, row)) if cols else {}
            scripts.append(
                {
                    "file_id": str(item.get("scriptfile", item.get("id", ""))),
                    "name": item.get("name", "unknown_script"),
                    "script_id": item.get("scriptid", ""),
                    "script_type": item.get("scripttype", ""),
                    "source": "custom_script",
                }
            )
        logger.info("suitescript_sync.scripts_discovered", count=len(scripts))
    except Exception:
        logger.warning("suitescript_sync.script_discovery_failed", exc_info=True)

    return files + scripts


# --- Content Fetching ---


async def fetch_file_content(
    file_id: str,
    access_token: str,
    account_id: str,
    db: AsyncSession | None = None,
    tenant_id: uuid.UUID | None = None,
    connection_id: uuid.UUID | None = None,
) -> str | None:
    """Fetch a single file's content from NetSuite REST API.

    Returns decoded UTF-8 content, or None on failure.
    """
    import time as _time

    slug = _normalize_account_id(account_id)
    url = f"https://{slug}.suitetalk.api.netsuite.com/services/rest/record/v1/file/{file_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    t0 = _time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            resp.raise_for_status()

        data = resp.json()

        # Log successful API call
        if db and tenant_id:
            from app.services.netsuite_api_logger import log_netsuite_request

            await log_netsuite_request(
                db=db,
                tenant_id=tenant_id,
                connection_id=connection_id,
                method="GET",
                url=url,
                response_status=resp.status_code,
                response_time_ms=elapsed_ms,
                source="suitescript_sync",
            )

        content_b64 = data.get("content", "")
        if not content_b64:
            return None

        raw_bytes = base64.b64decode(content_b64)

        # Enforce size limit
        if len(raw_bytes) > MAX_FILE_SIZE:
            truncated = raw_bytes[:MAX_FILE_SIZE]
            text = truncated.decode("utf-8", errors="replace")
            return f"// WARNING: File truncated from {len(raw_bytes)} bytes to {MAX_FILE_SIZE} bytes\n{text}"

        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("suitescript_sync.non_utf8_file", file_id=file_id)
        return None
    except Exception as exc:
        elapsed_ms = int((_time.monotonic() - t0) * 1000)
        # Log failed API call
        if db and tenant_id:
            from app.services.netsuite_api_logger import log_netsuite_request

            await log_netsuite_request(
                db=db,
                tenant_id=tenant_id,
                connection_id=connection_id,
                method="GET",
                url=url,
                response_status=getattr(getattr(exc, "response", None), "status_code", None),
                response_time_ms=elapsed_ms,
                error_message=str(exc),
                source="suitescript_sync",
            )
        logger.warning("suitescript_sync.fetch_failed", file_id=file_id, exc_info=True)
        return None


async def batch_fetch_contents(
    files: list[dict[str, Any]],
    access_token: str,
    account_id: str,
    db: AsyncSession | None = None,
    tenant_id: uuid.UUID | None = None,
    connection_id: uuid.UUID | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Fetch file contents in batches with rate limiting.

    Returns: (success_dict[file_id → content], failed_file_ids)
    """
    import time as _time

    contents: dict[str, str] = {}
    failed: list[str] = []
    t0 = _time.monotonic()

    for i in range(0, len(files), BATCH_SIZE):
        batch = files[i : i + BATCH_SIZE]

        # Don't pass db to individual tasks — asyncio.gather with shared session is unsafe
        tasks = [fetch_file_content(f["file_id"], access_token, account_id) for f in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for file_meta, result in zip(batch, results):
            fid = file_meta["file_id"]
            if isinstance(result, Exception) or result is None:
                failed.append(fid)
            else:
                contents[fid] = result

        # Rate limit delay between batches
        if i + BATCH_SIZE < len(files):
            await asyncio.sleep(BATCH_DELAY_SECONDS)

        logger.info(
            "suitescript_sync.batch_progress",
            fetched=len(contents),
            failed=len(failed),
            remaining=max(0, len(files) - i - BATCH_SIZE),
        )

    # Log a single summary entry after all batches complete
    if db and tenant_id:
        from app.services.netsuite_api_logger import log_netsuite_request

        total_elapsed_ms = int((_time.monotonic() - t0) * 1000)
        await log_netsuite_request(
            db=db,
            tenant_id=tenant_id,
            connection_id=connection_id,
            method="GET",
            url=f"batch_fetch ({len(files)} files)",
            response_status=200 if not failed else 207,
            response_time_ms=total_elapsed_ms,
            error_message=f"{len(failed)} files failed" if failed else None,
            source="suitescript_sync_batch",
        )

    return contents, failed


# --- Workspace Sync ---


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _build_file_path(file_meta: dict[str, Any]) -> str:
    """Build a workspace path from file metadata."""
    name = file_meta.get("name", "unknown.js")
    # Sanitize name: only alphanumeric, dots, underscores, hyphens
    safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)
    if not safe_name.endswith(".js"):
        safe_name = f"{safe_name}.js"

    source = file_meta.get("source", "file_cabinet")
    if source == "custom_script":
        script_id = file_meta.get("script_id", "")
        prefix = f"{script_id}_" if script_id else ""
        return f"CustomScripts/{prefix}{safe_name}"
    else:
        folder = file_meta.get("folder", "")
        folder_name = str(folder) if folder else "Uncategorized"
        # Sanitize folder name
        safe_folder = "".join(c if c.isalnum() or c in "._- " else "_" for c in folder_name)
        return f"SuiteScripts/{safe_folder}/{safe_name}"


async def _get_or_create_workspace(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID | None,
) -> Workspace:
    """Get or create the dedicated 'NetSuite Scripts' workspace for a tenant."""
    result = await db.execute(
        select(Workspace).where(
            Workspace.tenant_id == tenant_id,
            Workspace.name == WORKSPACE_NAME,
            Workspace.status == "active",
        )
    )
    ws = result.scalar_one_or_none()
    if ws:
        return ws

    ws = Workspace(
        tenant_id=tenant_id,
        name=WORKSPACE_NAME,
        description="Auto-synced SuiteScript files from your NetSuite account.",
        status="active",
        created_by=created_by or uuid.UUID(int=0),
    )
    db.add(ws)
    await db.flush()
    return ws


async def _upsert_workspace_file(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    path: str,
    content: str,
    netsuite_file_id: str | None = None,
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
        if netsuite_file_id and not existing.netsuite_file_id:
            existing.netsuite_file_id = netsuite_file_id
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
    )
    db.add(wf)
    return wf


async def _ensure_directories(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    paths: set[str],
) -> None:
    """Ensure all parent directories exist as WorkspaceFile directory records."""
    dirs_needed: set[str] = set()
    for path in paths:
        parts = PurePosixPath(path).parts
        for i in range(1, len(parts)):
            dirs_needed.add("/".join(parts[:i]))

    for dir_path in sorted(dirs_needed):
        result = await db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.workspace_id == workspace_id,
                WorkspaceFile.path == dir_path,
                WorkspaceFile.is_directory.is_(True),
            )
        )
        if not result.scalar_one_or_none():
            db.add(
                WorkspaceFile(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    path=dir_path,
                    file_name=PurePosixPath(dir_path).name,
                    is_directory=True,
                    size_bytes=0,
                )
            )

    await db.flush()


# --- Main Orchestrator ---


async def sync_scripts_to_workspace(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    access_token: str,
    account_id: str,
    user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Full sync: discover → fetch → upsert into workspace.

    Returns summary dict with status, counts, and workspace_id.
    """
    start_time = datetime.now(timezone.utc)

    # 1. Get or create workspace
    ws = await _get_or_create_workspace(db, tenant_id, user_id)

    # 2. Get or create sync state
    result = await db.execute(select(ScriptSyncState).where(ScriptSyncState.tenant_id == tenant_id))
    sync_state = result.scalar_one_or_none()
    if not sync_state:
        sync_state = ScriptSyncState(
            tenant_id=tenant_id,
            workspace_id=ws.id,
            connection_id=connection_id,
        )
        db.add(sync_state)
        await db.flush()

    sync_state.status = "in_progress"
    sync_state.error_message = None
    sync_state.workspace_id = ws.id
    sync_state.connection_id = connection_id
    await db.flush()

    try:
        # 3. Discover scripts
        discovered = await discover_scripts(access_token, account_id)
        sync_state.discovered_file_count = len(discovered)
        await db.flush()

        if not discovered:
            sync_state.status = "completed"
            sync_state.last_sync_at = datetime.now(timezone.utc)
            sync_state.total_files_loaded = 0
            sync_state.failed_files_count = 0
            await db.flush()
            return {
                "status": "completed",
                "workspace_id": str(ws.id),
                "files_loaded": 0,
                "files_failed": 0,
                "discovered": 0,
            }

        # 4. Batch fetch content
        contents, failed_ids = await batch_fetch_contents(
            discovered,
            access_token,
            account_id,
            db=db,
            tenant_id=tenant_id,
            connection_id=connection_id,
        )

        # 5. Build file paths and upsert
        file_paths: set[str] = set()
        loaded = 0

        for file_meta in discovered:
            fid = file_meta["file_id"]
            content = contents.get(fid)
            if content is None:
                continue

            path = _build_file_path(file_meta)
            file_paths.add(path)
            await _upsert_workspace_file(
                db,
                tenant_id,
                ws.id,
                path,
                content,
                netsuite_file_id=fid,
            )
            loaded += 1

        # 6. Ensure directories exist
        await _ensure_directories(db, tenant_id, ws.id, file_paths)

        # 7. Update sync state
        elapsed_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
        sync_state.status = "completed"
        sync_state.last_sync_at = datetime.now(timezone.utc)
        sync_state.total_files_loaded = loaded
        sync_state.failed_files_count = len(failed_ids)
        await db.flush()

        logger.info(
            "suitescript_sync.completed",
            tenant_id=str(tenant_id),
            files_loaded=loaded,
            files_failed=len(failed_ids),
            discovered=len(discovered),
            duration_ms=elapsed_ms,
        )

        return {
            "status": "completed",
            "workspace_id": str(ws.id),
            "files_loaded": loaded,
            "files_failed": len(failed_ids),
            "discovered": len(discovered),
            "duration_ms": elapsed_ms,
        }

    except Exception as exc:
        sync_state.status = "failed"
        sync_state.error_message = str(exc)[:2000]
        await db.flush()
        logger.error("suitescript_sync.failed", tenant_id=str(tenant_id), error=str(exc), exc_info=True)
        raise


async def get_sync_status(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any] | None:
    """Return the current sync state for a tenant, or None if never synced."""
    result = await db.execute(select(ScriptSyncState).where(ScriptSyncState.tenant_id == tenant_id))
    state = result.scalar_one_or_none()
    if not state:
        return None

    return {
        "status": state.status,
        "last_sync_at": state.last_sync_at.isoformat() if state.last_sync_at else None,
        "total_files_loaded": state.total_files_loaded,
        "discovered_file_count": state.discovered_file_count,
        "failed_files_count": state.failed_files_count,
        "error_message": state.error_message,
        "workspace_id": str(state.workspace_id),
    }
