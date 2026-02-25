"""Seed RAG DocChunk records from workspace SuiteScript source code.

Chunks .js files by SuiteScript entry-point functions (beforeSubmit,
getInputData, map, reduce, etc.) and creates tenant-specific DocChunks
for fast semantic search of business logic.
"""

from __future__ import annotations

import re
import uuid

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import DocChunk
from app.models.workspace import Workspace, WorkspaceFile
from app.services.chat.embeddings import embed_texts

logger = structlog.get_logger()

_SOURCE_PREFIX = "workspace_scripts/"
_MAX_CHUNK_CHARS = 6000

# ──────────────────────────────────────────────────────────────────
# SuiteScript entry point names by script type
# ──────────────────────────────────────────────────────────────────

_ENTRY_POINT_NAMES = frozenset({
    # UserEvent
    "beforeLoad", "beforeSubmit", "afterSubmit",
    # MapReduce
    "getInputData", "map", "reduce", "summarize",
    # Restlet
    "get", "post", "put", "delete",
    # Suitelet / Portlet
    "onRequest", "render",
    # Client Script
    "pageInit", "fieldChanged", "postSourcing", "sublistChanged",
    "lineInit", "validateField", "validateLine", "validateInsert",
    "validateDelete", "saveRecord",
    # Scheduled
    "execute",
    # Bundle Installation
    "afterInstall", "afterUpdate", "beforeInstall", "beforeUpdate",
    "beforeUninstall",
    # Workflow Action
    "onAction",
    # Mass Update
    "each",
})  # noqa: E501

# Regex patterns for entry point function declarations
# Pattern 1: const/let/var name = (params) => {
# Pattern 2: const/let/var name = function(params) {
# Pattern 3: function name(params) {
# Pattern 4: name: function(params) {   (object literal)
# Pattern 5: name(params) {             (shorthand method in object)
_EP_NAMES_PATTERN = "|".join(re.escape(n) for n in sorted(_ENTRY_POINT_NAMES))
_ENTRY_POINT_RE = re.compile(
    rf"(?:"
    rf"(?:const|let|var)\s+({_EP_NAMES_PATTERN})\s*="  # Pattern 1-2
    rf"|function\s+({_EP_NAMES_PATTERN})\s*\("          # Pattern 3
    rf"|({_EP_NAMES_PATTERN})\s*:\s*function\s*\("      # Pattern 4
    rf"|({_EP_NAMES_PATTERN})\s*\([^)]*\)\s*\{{"         # Pattern 5
    rf")",
    re.MULTILINE,
)


# ──────────────────────────────────────────────────────────────────
# Detection and chunking helpers
# ──────────────────────────────────────────────────────────────────


def _detect_script_type(content: str) -> str | None:
    """Extract @NScriptType from JSDoc annotation."""
    m = re.search(r"@NScriptType\s+(\w+)", content)
    return m.group(1) if m else None


def _find_entry_points(content: str) -> list[tuple[str, int]]:
    """Find SuiteScript entry point functions and their character offsets.

    Returns list of (name, offset) sorted by offset.
    """
    results: list[tuple[str, int]] = []
    seen: set[str] = set()

    for m in _ENTRY_POINT_RE.finditer(content):
        # One of the 4 groups will have the name
        name = m.group(1) or m.group(2) or m.group(3) or m.group(4)
        if name and name not in seen:
            results.append((name, m.start()))
            seen.add(name)

    results.sort(key=lambda x: x[1])
    return results


def _chunk_by_entry_points(
    content: str,
    filepath: str,
    script_type: str | None,
) -> list[tuple[str, str, str]]:
    """Split file into chunks at entry point boundaries.

    Returns list of (title, source_path_suffix, chunk_content).
    Each chunk gets a contextual header prepended.

    Fallback: files with no entry points -> single whole-file chunk.
    """
    entry_points = _find_entry_points(content)

    if not entry_points:
        # Whole file as one chunk
        chunk = _build_chunk_content(content, filepath, script_type, None)
        return [(filepath, filepath, chunk)]

    chunks: list[tuple[str, str, str]] = []

    for i, (name, offset) in enumerate(entry_points):
        # Extract from this entry point to the next (or end of file)
        end = entry_points[i + 1][1] if i + 1 < len(entry_points) else len(content)
        section = content[offset:end].rstrip()

        chunk = _build_chunk_content(section, filepath, script_type, name)
        title = f"{filepath}#{name}"
        source_suffix = f"{filepath}#{name}"
        chunks.append((title, source_suffix, chunk))

    return chunks


def _build_chunk_content(
    code: str,
    filepath: str,
    script_type: str | None,
    entry_point: str | None,
) -> str:
    """Build chunk content with contextual header."""
    header_lines = [f"// File: {filepath}"]
    if script_type:
        header_lines.append(f"// Script Type: {script_type}")
    if entry_point:
        header_lines.append(f"// Entry Point: {entry_point}")
    header_lines.append("")

    header = "\n".join(header_lines)
    full = header + code

    if len(full) > _MAX_CHUNK_CHARS:
        full = full[:_MAX_CHUNK_CHARS] + "\n// ... (truncated)"

    return full


# ──────────────────────────────────────────────────────────────────
# Main seeding function
# ──────────────────────────────────────────────────────────────────


async def seed_workspace_scripts(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID | None = None,
    force: bool = False,
) -> int:
    """Chunk and seed workspace .js files into DocChunks.

    Incremental by default: compares sha256_hash stored in DocChunk
    metadata_ against WorkspaceFile.sha256_hash. Only re-chunks changed files.

    force=True: deletes ALL workspace_scripts/ chunks for tenant, re-processes everything.

    Returns number of chunks created.
    """
    # 1. Find the target workspace
    if workspace_id is None:
        ws_result = await db.execute(
            select(Workspace.id)
            .where(Workspace.tenant_id == tenant_id, Workspace.status == "active")
            .order_by(Workspace.created_at.desc())
            .limit(1)
        )
        workspace_id = ws_result.scalar_one_or_none()
        if workspace_id is None:
            return 0

    # 2. Load .js files from workspace
    file_result = await db.execute(
        select(WorkspaceFile).where(
            WorkspaceFile.tenant_id == tenant_id,
            WorkspaceFile.workspace_id == workspace_id,
            WorkspaceFile.is_directory.is_(False),
            WorkspaceFile.path.ilike("%.js"),
        )
    )
    files = file_result.scalars().all()

    if not files:
        return 0

    # 3. If force mode, delete all existing workspace script chunks
    if force:
        await db.execute(
            delete(DocChunk).where(
                DocChunk.tenant_id == tenant_id,
                DocChunk.source_path.like(f"{_SOURCE_PREFIX}%"),
            )
        )
        existing_hashes: dict[str, str] = {}
    else:
        # Load existing chunk hashes for incremental comparison
        existing_result = await db.execute(
            select(DocChunk.source_path, DocChunk.metadata_).where(
                DocChunk.tenant_id == tenant_id,
                DocChunk.source_path.like(f"{_SOURCE_PREFIX}%"),
            )
        )
        # Build filepath -> hash mapping from existing chunks
        existing_hashes = {}
        for row in existing_result.all():
            source_path = row[0]
            meta = row[1] or {}
            # Extract filepath from source_path (strip prefix and #entry_point)
            fp = source_path.removeprefix(_SOURCE_PREFIX).split("#")[0]
            if fp not in existing_hashes and meta.get("sha256_hash"):
                existing_hashes[fp] = meta["sha256_hash"]

    # 4. Process each file
    total_chunks = 0
    all_new_chunks: list[tuple[str, str, str, str | None, str | None, str | None]] = []
    # (source_path, title, content, sha256_hash, script_type, entry_point)

    for f in files:
        if not f.content or not f.path:
            continue

        filepath = f.path

        # Incremental check: skip if hash unchanged
        if not force and filepath in existing_hashes:
            if existing_hashes[filepath] == f.sha256_hash:
                continue

        # Delete existing chunks for this file (if any, for incremental updates)
        if not force:
            await db.execute(
                delete(DocChunk).where(
                    DocChunk.tenant_id == tenant_id,
                    DocChunk.source_path.like(f"{_SOURCE_PREFIX}{filepath}%"),
                )
            )

        # Chunk the file
        script_type = _detect_script_type(f.content)
        chunks = _chunk_by_entry_points(f.content, filepath, script_type)

        for title, source_suffix, chunk_content in chunks:
            entry_point = source_suffix.split("#")[1] if "#" in source_suffix else None
            all_new_chunks.append((
                f"{_SOURCE_PREFIX}{source_suffix}",
                title,
                chunk_content,
                f.sha256_hash,
                script_type,
                entry_point,
            ))

    if not all_new_chunks:
        return 0

    # 5. Batch embed all chunks
    texts = [c[2] for c in all_new_chunks]
    embeddings = await embed_texts(texts)

    # 6. Create DocChunk records
    for i, (source_path, title, content, sha256_hash, script_type, entry_point) in enumerate(all_new_chunks):
        chunk = DocChunk(
            tenant_id=tenant_id,
            source_path=source_path,
            title=title,
            chunk_index=0,
            content=content,
            token_count=len(content.split()),
            embedding=embeddings[i] if embeddings else None,
            metadata_={
                "type": "workspace_script",
                "sha256_hash": sha256_hash,
                "workspace_id": str(workspace_id),
                "script_type": script_type,
                "entry_point": entry_point,
            },
        )
        db.add(chunk)
        total_chunks += 1

    await db.flush()

    logger.info(
        "workspace_rag.scripts_seeded",
        tenant_id=str(tenant_id),
        workspace_id=str(workspace_id),
        files_processed=len({c[0].split("#")[0] for c in all_new_chunks}),
        chunks_created=total_chunks,
        embedded=embeddings is not None,
    )

    return total_chunks
