"""Reorganize existing workspace files by SuiteScript type.

Reads all .js files in a workspace, detects their script type from content/filename,
and moves them into type-based folders while preserving netsuite_file_id.

Idempotent: files already in the correct folder are skipped.
"""

from __future__ import annotations

import uuid
from pathlib import PurePosixPath

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace import WorkspaceFile
from app.services.script_type_detector import (
    get_folder_for_type,
    resolve_script_type,
)

logger = structlog.get_logger(__name__)


async def reorganize_workspace(
    db: AsyncSession,
    workspace_id: uuid.UUID,
) -> dict[str, int]:
    """Reorganize all .js files in a workspace by script type.

    Returns:
        {"moved": N, "skipped": N, "errors": N}
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

    for f in files:
        # Only process JavaScript files
        if not f.file_name.endswith(".js"):
            skipped += 1
            continue

        try:
            # Detect script type
            stype = resolve_script_type(
                content=f.content,
                filename=f.file_name,
            )

            # Build the target path
            folder = get_folder_for_type(stype)
            target_path = f"SuiteScripts/{folder}/{f.file_name}"

            # Skip if already in correct location
            if f.path == target_path and f.script_type == stype:
                skipped += 1
                continue

            # Update the file
            old_path = f.path
            f.path = target_path
            f.script_type = stype

            logger.debug(
                "workspace_reorganize.moved",
                file_name=f.file_name,
                old_path=old_path,
                new_path=target_path,
                script_type=stype,
            )
            moved += 1

        except Exception as e:
            logger.error(
                "workspace_reorganize.error",
                file_id=str(f.id),
                file_name=f.file_name,
                error=str(e),
            )
            errors += 1

    # Clean up stale directory entries and create new ones
    if moved > 0:
        await _rebuild_directories(db, workspace_id)

    logger.info(
        "workspace_reorganize.completed",
        workspace_id=str(workspace_id),
        moved=moved,
        skipped=skipped,
        errors=errors,
    )

    return {"moved": moved, "skipped": skipped, "errors": errors}


async def _rebuild_directories(
    db: AsyncSession,
    workspace_id: uuid.UUID,
) -> None:
    """Rebuild directory entries based on current file paths.

    Creates any missing parent directories and removes empty ones.
    """
    # Get all current file paths
    result = await db.execute(
        select(WorkspaceFile.path).where(
            WorkspaceFile.workspace_id == workspace_id,
            WorkspaceFile.is_directory.is_(False),
        )
    )
    file_paths = [r[0] for r in result.all()]

    # Compute needed directories
    dirs_needed: set[str] = set()
    for path in file_paths:
        parts = PurePosixPath(path).parts
        for i in range(1, len(parts)):
            dirs_needed.add("/".join(parts[:i]))

    # Get existing directory records
    result = await db.execute(
        select(WorkspaceFile).where(
            WorkspaceFile.workspace_id == workspace_id,
            WorkspaceFile.is_directory.is_(True),
        )
    )
    existing_dirs = {d.path: d for d in result.scalars().all()}

    # Create missing directories
    for dir_path in sorted(dirs_needed):
        if dir_path not in existing_dirs:
            # Get tenant_id from any existing file
            sample = await db.execute(
                select(WorkspaceFile.tenant_id).where(
                    WorkspaceFile.workspace_id == workspace_id,
                ).limit(1)
            )
            tenant_id = sample.scalar_one()

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

    # Remove empty directories (directories not in dirs_needed)
    for dir_path, dir_record in existing_dirs.items():
        if dir_path not in dirs_needed:
            await db.delete(dir_record)
