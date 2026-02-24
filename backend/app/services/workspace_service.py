"""Workspace service — file ops, changeset state machine, patch proposal/apply."""

from __future__ import annotations

import hashlib
import io
import mimetypes
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import structlog
import whatthepatch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import (
    Workspace,
    WorkspaceChangeSet,
    WorkspaceFile,
    WorkspacePatch,
)

logger = structlog.get_logger()

# --- Constants ---

MAX_FILE_SIZE = 256 * 1024  # 256 KB per file
MAX_PATH_LENGTH = 512
MAX_PATH_DEPTH = 20
MAX_READ_CHARS = 32_000
MAX_DIFF_SIZE = 256 * 1024  # 256 KB
MAX_IMPORT_FILES = 2000
SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9_./ \-]+$")
LOCK_EXPIRY_MINUTES = 30

CHANGESET_TRANSITIONS: dict[str, dict[str, str]] = {
    "draft": {"submit": "pending_review", "discard": "rejected"},
    "pending_review": {"approve": "approved", "reject": "rejected", "revert": "draft"},
    "approved": {"apply": "applied", "revoke": "draft"},
    "applied": {},
    "rejected": {},
}


# --- Path Validation ---


def validate_path(path: str) -> str:
    """Validate and normalize a virtual file path. Raises ValueError on bad input."""
    if not path or len(path) > MAX_PATH_LENGTH:
        raise ValueError(f"Path must be 1-{MAX_PATH_LENGTH} characters")

    normalized = str(PurePosixPath(path))
    if ".." in normalized.split("/"):
        raise ValueError("Path traversal sequences ('..') are not allowed")
    if normalized.startswith("/"):
        raise ValueError("Absolute paths are not allowed")
    if not SAFE_PATH_RE.match(normalized):
        raise ValueError("Path contains disallowed characters")
    if normalized.count("/") >= MAX_PATH_DEPTH:
        raise ValueError(f"Path exceeds maximum depth of {MAX_PATH_DEPTH}")

    return normalized


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# --- File Operations ---


async def create_workspace(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    name: str,
    created_by: uuid.UUID,
    description: str | None = None,
) -> Workspace:
    ws = Workspace(
        tenant_id=tenant_id,
        name=name,
        description=description,
        status="active",
        created_by=created_by,
    )
    db.add(ws)
    await db.flush()
    return ws


async def get_workspace(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> Workspace | None:
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def list_workspaces(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> list[Workspace]:
    result = await db.execute(
        select(Workspace)
        .where(Workspace.tenant_id == tenant_id, Workspace.status == "active")
        .order_by(Workspace.created_at.desc())
    )
    return list(result.scalars().all())


async def archive_workspace(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> Workspace | None:
    ws = await get_workspace(db, workspace_id, tenant_id)
    if ws:
        ws.status = "archived"
        await db.flush()
    return ws


async def import_workspace(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID,
    zip_bytes: bytes,
) -> dict[str, Any]:
    """Import files from a zip archive into a workspace."""
    ws = await get_workspace(db, workspace_id, tenant_id)
    if not ws:
        raise ValueError("Workspace not found")

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise ValueError("Invalid zip file")

    file_count = 0
    skipped = 0
    dirs_created: set[str] = set()

    for info in zf.infolist():
        if file_count >= MAX_IMPORT_FILES:
            break

        # Skip __MACOSX and hidden files
        if info.filename.startswith("__MACOSX") or "/." in info.filename:
            skipped += 1
            continue

        try:
            path = validate_path(info.filename)
        except ValueError:
            skipped += 1
            continue

        if info.is_dir():
            if path not in dirs_created:
                dirs_created.add(path)
                wf = WorkspaceFile(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    path=path.rstrip("/"),
                    file_name=PurePosixPath(path).name or path,
                    is_directory=True,
                    size_bytes=0,
                )
                db.add(wf)
                file_count += 1
            continue

        # Ensure parent dirs exist
        parts = PurePosixPath(path).parts
        for i in range(1, len(parts)):
            dir_path = "/".join(parts[:i])
            if dir_path not in dirs_created:
                dirs_created.add(dir_path)
                wf = WorkspaceFile(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    path=dir_path,
                    file_name=parts[i - 1],
                    is_directory=True,
                    size_bytes=0,
                )
                db.add(wf)
                file_count += 1

        # Read file content
        raw = zf.read(info.filename)
        if len(raw) > MAX_FILE_SIZE:
            skipped += 1
            continue

        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            skipped += 1
            continue

        mime, _ = mimetypes.guess_type(path)
        wf = WorkspaceFile(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            path=path,
            file_name=PurePosixPath(path).name,
            mime_type=mime,
            size_bytes=len(raw),
            content=content,
            sha256_hash=_sha256(content),
            is_directory=False,
        )
        db.add(wf)
        file_count += 1

    await db.flush()
    return {"imported": file_count, "skipped": skipped}


async def list_files(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID,
    prefix: str | None = None,
    recursive: bool = True,
) -> list[dict]:
    """Return file tree as a flat list (frontend builds tree from paths)."""
    q = select(WorkspaceFile).where(
        WorkspaceFile.workspace_id == workspace_id,
        WorkspaceFile.tenant_id == tenant_id,
    )
    if prefix:
        q = q.where(WorkspaceFile.path.ilike(f"{prefix}%"))
    q = q.order_by(WorkspaceFile.is_directory.desc(), WorkspaceFile.path)

    result = await db.execute(q)
    files = result.scalars().all()

    # Build tree structure
    return _build_tree(files)


def _build_tree(files: list[WorkspaceFile]) -> list[dict]:
    """Convert flat file list to nested tree structure."""
    nodes: dict[str, dict] = {}
    roots: list[dict] = []

    for f in files:
        node = {
            "id": str(f.id),
            "name": f.file_name,
            "path": f.path,
            "is_directory": f.is_directory,
            "size_bytes": f.size_bytes if not f.is_directory else None,
            "children": [] if f.is_directory else None,
        }
        nodes[f.path] = node

    for f in files:
        node = nodes[f.path]
        parent_path = str(PurePosixPath(f.path).parent)
        if parent_path == "." or parent_path not in nodes:
            roots.append(node)
        else:
            parent = nodes[parent_path]
            if parent["children"] is not None:
                parent["children"].append(node)

    return roots


async def read_file(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    tenant_id: uuid.UUID,
    line_start: int = 1,
    line_end: int | None = None,
) -> dict | None:
    result = await db.execute(
        select(WorkspaceFile).where(
            WorkspaceFile.id == file_id,
            WorkspaceFile.workspace_id == workspace_id,
            WorkspaceFile.tenant_id == tenant_id,
            WorkspaceFile.is_directory.is_(False),
        )
    )
    f = result.scalar_one_or_none()
    if not f:
        return None

    content = f.content or ""
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)

    # Apply line range
    start_idx = max(0, line_start - 1)
    end_idx = line_end if line_end else len(lines)
    sliced = lines[start_idx:end_idx]
    sliced_content = "".join(sliced)

    truncated = False
    if len(sliced_content) > MAX_READ_CHARS:
        sliced_content = sliced_content[:MAX_READ_CHARS]
        truncated = True
    elif end_idx < total_lines:
        truncated = True

    return {
        "id": str(f.id),
        "path": f.path,
        "file_name": f.file_name,
        "content": sliced_content,
        "truncated": truncated,
        "total_lines": total_lines,
        "mime_type": f.mime_type,
    }


async def search_files(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID,
    query: str,
    search_type: str = "filename",
    limit: int = 20,
) -> list[dict]:
    """Search files by filename (path ILIKE) or content (content ILIKE)."""
    limit = min(limit, 50)
    results = []

    if search_type == "content":
        q = (
            select(WorkspaceFile)
            .where(
                WorkspaceFile.workspace_id == workspace_id,
                WorkspaceFile.tenant_id == tenant_id,
                WorkspaceFile.is_directory.is_(False),
                WorkspaceFile.content.ilike(f"%{query}%"),
            )
            .limit(limit)
        )
        result = await db.execute(q)
        files = result.scalars().all()

        for f in files:
            if not f.content:
                continue
            for i, line in enumerate(f.content.splitlines(), 1):
                if query.lower() in line.lower():
                    results.append(
                        {
                            "file_id": str(f.id),
                            "path": f.path,
                            "line_number": i,
                            "snippet": line.strip()[:200],
                            "context": f.path,
                        }
                    )
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break
    else:
        # filename search
        q = (
            select(WorkspaceFile)
            .where(
                WorkspaceFile.workspace_id == workspace_id,
                WorkspaceFile.tenant_id == tenant_id,
                WorkspaceFile.path.ilike(f"%{query}%"),
            )
            .order_by(WorkspaceFile.is_directory.desc(), WorkspaceFile.path)
            .limit(limit)
        )
        result = await db.execute(q)
        files = result.scalars().all()

        for f in files:
            results.append(
                {
                    "file_id": str(f.id),
                    "path": f.path,
                    "line_number": 0,
                    "snippet": f.file_name,
                    "context": f.path,
                }
            )

    return results


# --- Changeset Operations ---


async def create_changeset(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID,
    title: str,
    proposed_by: uuid.UUID,
    description: str | None = None,
) -> WorkspaceChangeSet:
    cs = WorkspaceChangeSet(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        title=title,
        description=description,
        status="draft",
        proposed_by=proposed_by,
    )
    db.add(cs)
    await db.flush()
    return cs


async def get_changeset(
    db: AsyncSession,
    changeset_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> WorkspaceChangeSet | None:
    result = await db.execute(
        select(WorkspaceChangeSet).where(
            WorkspaceChangeSet.id == changeset_id,
            WorkspaceChangeSet.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def list_changesets(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> list[WorkspaceChangeSet]:
    result = await db.execute(
        select(WorkspaceChangeSet)
        .where(
            WorkspaceChangeSet.workspace_id == workspace_id,
            WorkspaceChangeSet.tenant_id == tenant_id,
        )
        .order_by(WorkspaceChangeSet.created_at.desc())
    )
    return list(result.scalars().all())


async def transition_changeset(
    db: AsyncSession,
    changeset_id: uuid.UUID,
    tenant_id: uuid.UUID,
    action: str,
    actor_id: uuid.UUID,
    rejection_reason: str | None = None,
) -> WorkspaceChangeSet:
    cs = await get_changeset(db, changeset_id, tenant_id)
    if not cs:
        raise ValueError("Changeset not found")

    allowed = CHANGESET_TRANSITIONS.get(cs.status, {})
    if action not in allowed:
        raise ValueError(f"Action '{action}' not valid for status '{cs.status}'. Allowed: {list(allowed.keys())}")

    new_status = allowed[action]
    cs.status = new_status
    now = datetime.now(timezone.utc)

    if action in ("approve", "reject"):
        cs.reviewed_by = actor_id
        cs.reviewed_at = now
    if action == "reject" and rejection_reason:
        cs.rejection_reason = rejection_reason

    # Release file locks when changeset is rejected or discarded
    if new_status == "rejected":
        await release_file_locks(db, changeset_id, tenant_id)

    await db.flush()
    return cs


async def apply_changeset(
    db: AsyncSession,
    changeset_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> WorkspaceChangeSet:
    """Apply an approved changeset to workspace files."""
    # Lock the changeset row to prevent concurrent applies
    cs_result = await db.execute(
        select(WorkspaceChangeSet)
        .where(
            WorkspaceChangeSet.id == changeset_id,
            WorkspaceChangeSet.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    cs = cs_result.scalar_one_or_none()
    if not cs:
        raise ValueError("Changeset not found")
    if cs.status != "approved":
        raise ValueError(f"Changeset must be approved before applying (current: {cs.status})")

    # Load patches ordered by apply_order
    result = await db.execute(
        select(WorkspacePatch).where(WorkspacePatch.changeset_id == changeset_id).order_by(WorkspacePatch.apply_order)
    )
    patches = list(result.scalars().all())

    for patch in patches:
        if patch.operation == "create":
            # Create new file
            path = validate_path(patch.file_path)
            content = patch.new_content or ""
            wf = WorkspaceFile(
                tenant_id=tenant_id,
                workspace_id=cs.workspace_id,
                path=path,
                file_name=PurePosixPath(path).name,
                size_bytes=len(content.encode("utf-8")),
                content=content,
                sha256_hash=_sha256(content),
                is_directory=False,
            )
            db.add(wf)

        elif patch.operation == "delete":
            file_result = await db.execute(
                select(WorkspaceFile)
                .where(
                    WorkspaceFile.workspace_id == cs.workspace_id,
                    WorkspaceFile.path == patch.file_path,
                    WorkspaceFile.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            wf = file_result.scalar_one_or_none()
            if wf:
                await db.delete(wf)

        elif patch.operation == "modify":
            file_result = await db.execute(
                select(WorkspaceFile)
                .where(
                    WorkspaceFile.workspace_id == cs.workspace_id,
                    WorkspaceFile.path == patch.file_path,
                    WorkspaceFile.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            wf = file_result.scalar_one_or_none()
            if not wf:
                raise ValueError(f"File not found for modify: {patch.file_path}")

            # Conflict detection
            current_hash = wf.sha256_hash or ""
            if current_hash != patch.baseline_sha256:
                raise ValueError(f"Conflict detected on {patch.file_path}: file was modified since patch was proposed")

            # Apply diff
            if patch.unified_diff:
                new_content = _apply_diff(wf.content or "", patch.unified_diff)
            elif patch.new_content is not None:
                new_content = patch.new_content
            else:
                raise ValueError(f"No diff or content for modify patch on {patch.file_path}")

            wf.content = new_content
            wf.sha256_hash = _sha256(new_content)
            wf.size_bytes = len(new_content.encode("utf-8"))

    # Release all file locks held by this changeset
    for patch in patches:
        file_result = await db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.workspace_id == cs.workspace_id,
                WorkspaceFile.path == patch.file_path,
                WorkspaceFile.tenant_id == tenant_id,
            )
        )
        wf = file_result.scalar_one_or_none()
        if wf and wf.locked_by:
            wf.locked_by = None
            wf.locked_at = None

    cs.status = "applied"
    cs.applied_by = actor_id
    cs.applied_at = datetime.now(timezone.utc)
    await db.flush()
    return cs


def _apply_diff(original: str, unified_diff: str) -> str:
    """Apply a unified diff using whatthepatch."""
    diffs = list(whatthepatch.parse_patch(unified_diff))
    if not diffs:
        raise ValueError("Could not parse unified diff")

    diff = diffs[0]
    result = whatthepatch.apply_diff(diff, original)
    if result is None:
        raise ValueError("Failed to apply diff — lines may have changed")

    # whatthepatch may return either a list[str] or a tuple(list[str], metadata)
    if isinstance(result, tuple):
        applied_lines = result[0]
    else:
        applied_lines = result
    return "\n".join(applied_lines) + "\n" if applied_lines else ""


async def get_changeset_diff(
    db: AsyncSession,
    changeset_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> dict | None:
    """Return diff view data for a changeset."""
    cs = await get_changeset(db, changeset_id, tenant_id)
    if not cs:
        return None

    result = await db.execute(
        select(WorkspacePatch).where(WorkspacePatch.changeset_id == changeset_id).order_by(WorkspacePatch.apply_order)
    )
    patches = list(result.scalars().all())

    files = []
    for patch in patches:
        original = ""
        modified = ""

        if patch.operation == "create":
            modified = patch.new_content or ""
        elif patch.operation == "delete":
            # Get current content
            file_result = await db.execute(
                select(WorkspaceFile).where(
                    WorkspaceFile.workspace_id == cs.workspace_id,
                    WorkspaceFile.path == patch.file_path,
                    WorkspaceFile.tenant_id == tenant_id,
                )
            )
            wf = file_result.scalar_one_or_none()
            original = wf.content if wf else ""
        elif patch.operation == "modify":
            file_result = await db.execute(
                select(WorkspaceFile).where(
                    WorkspaceFile.workspace_id == cs.workspace_id,
                    WorkspaceFile.path == patch.file_path,
                    WorkspaceFile.tenant_id == tenant_id,
                )
            )
            wf = file_result.scalar_one_or_none()
            original = (wf.content if wf else "") or ""
            if patch.unified_diff:
                try:
                    modified = _apply_diff(original, patch.unified_diff)
                except ValueError:
                    modified = original  # fallback
            elif patch.new_content is not None:
                modified = patch.new_content
            else:
                modified = original

        files.append(
            {
                "file_path": patch.file_path,
                "operation": patch.operation,
                "original_content": original,
                "modified_content": modified,
            }
        )

    return {
        "changeset_id": str(cs.id),
        "title": cs.title,
        "files": files,
    }


# --- Patch Operations ---


def _is_lock_expired(locked_at: datetime | None) -> bool:
    """Check if a file lock has expired (older than LOCK_EXPIRY_MINUTES)."""
    if not locked_at:
        return True
    from datetime import timedelta
    return datetime.now(timezone.utc) - locked_at > timedelta(minutes=LOCK_EXPIRY_MINUTES)


async def release_file_locks(
    db: AsyncSession,
    changeset_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """Release locks on all files affected by a changeset."""
    patch_result = await db.execute(
        select(WorkspacePatch.file_path).where(
            WorkspacePatch.changeset_id == changeset_id,
            WorkspacePatch.tenant_id == tenant_id,
        )
    )
    paths = [row[0] for row in patch_result.all()]
    if not paths:
        return

    # Look up the changeset to get workspace_id
    cs = await get_changeset(db, changeset_id, tenant_id)
    if not cs:
        return

    for path in paths:
        file_result = await db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.workspace_id == cs.workspace_id,
                WorkspaceFile.path == path,
                WorkspaceFile.tenant_id == tenant_id,
            )
        )
        wf = file_result.scalar_one_or_none()
        if wf and wf.locked_by:
            wf.locked_by = None
            wf.locked_at = None

    await db.flush()


async def propose_patch(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID,
    file_path: str,
    unified_diff: str,
    title: str,
    proposed_by: uuid.UUID,
    rationale: str | None = None,
) -> dict:
    """Create a draft changeset with a single patch for a file modification."""
    path = validate_path(file_path)

    if len(unified_diff) > MAX_DIFF_SIZE:
        raise ValueError(f"Diff exceeds maximum size of {MAX_DIFF_SIZE} bytes")

    # Find the target file (with FOR UPDATE to prevent races)
    file_result = await db.execute(
        select(WorkspaceFile)
        .where(
            WorkspaceFile.workspace_id == workspace_id,
            WorkspaceFile.path == path,
            WorkspaceFile.tenant_id == tenant_id,
            WorkspaceFile.is_directory.is_(False),
        )
        .with_for_update()
    )
    wf = file_result.scalar_one_or_none()

    if wf:
        # Check for existing lock by another user
        if wf.locked_by and wf.locked_by != proposed_by and not _is_lock_expired(wf.locked_at):
            raise ValueError(
                f"File '{path}' is locked by another user. "
                f"Lock acquired at {wf.locked_at.isoformat() if wf.locked_at else 'unknown'}. "
                f"Locks expire after {LOCK_EXPIRY_MINUTES} minutes."
            )
        # Acquire lock
        wf.locked_by = proposed_by
        wf.locked_at = datetime.now(timezone.utc)
        operation = "modify"
        baseline = wf.sha256_hash or _sha256(wf.content or "")
    else:
        operation = "create"
        baseline = _sha256("")

    # Preview
    preview_original = (wf.content if wf else "") or ""
    parse_error = None
    try:
        preview_modified = _apply_diff(preview_original, unified_diff)
        diff_status = "valid"
    except ValueError as e:
        preview_modified = preview_original
        diff_status = f"parse_error: {e}"
        parse_error = str(e)

    if operation == "create" and parse_error:
        raise ValueError(f"Invalid diff for create patch: {parse_error}")

    # Create changeset
    cs = await create_changeset(
        db,
        workspace_id,
        tenant_id,
        title,
        proposed_by,
        description=rationale,
    )

    # Create patch
    patch = WorkspacePatch(
        tenant_id=tenant_id,
        changeset_id=cs.id,
        file_path=path,
        operation=operation,
        unified_diff=unified_diff if operation == "modify" else None,
        new_content=preview_modified if operation == "create" else None,
        baseline_sha256=baseline,
        apply_order=0,
    )
    db.add(patch)
    await db.flush()

    return {
        "changeset_id": str(cs.id),
        "patch_id": str(patch.id),
        "operation": operation,
        "diff_status": diff_status,
        "diff_preview": {
            "file_path": path,
            "original_content": preview_original[:MAX_READ_CHARS],
            "modified_content": preview_modified[:MAX_READ_CHARS],
        },
        "risk_summary": f"Proposed {operation} on {path}",
    }


async def preview_patch(
    db: AsyncSession,
    patch_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> dict | None:
    """Preview the effect of a single patch."""
    result = await db.execute(
        select(WorkspacePatch).where(
            WorkspacePatch.id == patch_id,
            WorkspacePatch.tenant_id == tenant_id,
        )
    )
    patch = result.scalar_one_or_none()
    if not patch:
        return None

    cs = await get_changeset(db, patch.changeset_id, tenant_id)
    if not cs:
        return None

    original = ""
    if patch.operation in ("modify", "delete"):
        file_result = await db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.workspace_id == cs.workspace_id,
                WorkspaceFile.path == patch.file_path,
                WorkspaceFile.tenant_id == tenant_id,
            )
        )
        wf = file_result.scalar_one_or_none()
        original = (wf.content if wf else "") or ""

    modified = original
    if patch.operation == "modify" and patch.unified_diff:
        try:
            modified = _apply_diff(original, patch.unified_diff)
        except ValueError:
            pass
    elif patch.operation == "create":
        modified = patch.new_content or ""
    elif patch.operation == "delete":
        modified = ""

    return {
        "patch_id": str(patch.id),
        "file_path": patch.file_path,
        "operation": patch.operation,
        "original_content": original[:MAX_READ_CHARS],
        "modified_content": modified[:MAX_READ_CHARS],
    }


# --- Reindex ---


async def reindex_workspace(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> dict[str, Any]:
    """Reindex workspace files — recompute SHA256 hashes and fix mismatches."""
    ws = await get_workspace(db, workspace_id, tenant_id)
    if not ws:
        raise ValueError("Workspace not found")

    result = await db.execute(
        select(WorkspaceFile).where(
            WorkspaceFile.workspace_id == workspace_id,
            WorkspaceFile.tenant_id == tenant_id,
            WorkspaceFile.is_directory.is_(False),
        )
    )
    files = result.scalars().all()

    reindexed = 0
    hash_mismatches = 0
    for f in files:
        if f.content is not None:
            computed = _sha256(f.content)
            if f.sha256_hash != computed:
                f.sha256_hash = computed
                hash_mismatches += 1
            reindexed += 1

    await db.flush()
    return {
        "workspace_id": str(workspace_id),
        "files_reindexed": reindexed,
        "hash_mismatches_fixed": hash_mismatches,
    }
