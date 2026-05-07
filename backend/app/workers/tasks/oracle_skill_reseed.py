"""Celery Beat task: re-seed Oracle skills when skills-lock.json hashes change.

State is stored in sentinel DomainKnowledgeChunk rows (chunk_index = -1) so we
don't need a new table. Per-skill granularity prevents re-embedding all 7 when
only one Oracle skill changes upstream.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from app.core.database import async_session_factory
from app.models.domain_knowledge import DomainKnowledgeChunk
from app.services.oracle_skill_seeder import (
    SLUG_MAP,
    chunk_markdown,
    walk_oracle_skills,
)
from app.services.chat.domain_knowledge import embed_domain_texts
from app.workers.celery_app import celery_app

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _read_lockfile_hashes(root: Path) -> dict[str, str]:
    """Return {skill_name: computedHash} from skills-lock.json. Empty if missing."""
    lock_path = root / "skills-lock.json"
    if not lock_path.is_file():
        return {}
    data = json.loads(lock_path.read_text())
    return {name: meta.get("computedHash", "") for name, meta in data.get("skills", {}).items()}


async def _read_stored_hashes(db: "AsyncSession", partition_ids: list[str]) -> dict[str, str]:
    """Read sentinel rows holding the last-seeded hash per partition."""
    stmt = select(DomainKnowledgeChunk).where(
        DomainKnowledgeChunk.partition_id.in_(partition_ids),
        DomainKnowledgeChunk.chunk_index == -1,
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {row.partition_id: (row.topic_tags or {}).get("oracle_skill_hash", "") for row in rows}


async def _reseed_partition(
    db: "AsyncSession", root: Path, skill_name: str, slug: str, new_hash: str
) -> int:
    """Delete + re-insert one partition. Writes a sentinel row for hash state."""
    chunks: list[str] = []
    paths: list[Path] = []
    for found_slug, path, content in walk_oracle_skills(root):
        if found_slug != slug:
            continue
        for chunk in chunk_markdown(content):
            chunks.append(chunk)
            paths.append(path)

    if not chunks:
        logger.warning("No chunks for %s — skipping partition", slug)
        return 0

    # Delete existing rows (data + sentinel)
    await db.execute(delete(DomainKnowledgeChunk).where(DomainKnowledgeChunk.partition_id == slug))

    embeddings = await embed_domain_texts(chunks) or [None] * len(chunks)
    for idx, (path, chunk_text, vec) in enumerate(zip(paths, chunks, embeddings, strict=True)):
        db.add(DomainKnowledgeChunk(
            source_uri=f"{slug}/{path.name}#chunk-{idx}",
            chunk_index=idx,
            raw_text=chunk_text,
            token_count=len(chunk_text) // 4,
            source_type="oracle_skill",
            partition_id=slug,
            embedding=vec,
            is_deprecated=False,
        ))

    # Sentinel row for next-cycle hash comparison.
    db.add(DomainKnowledgeChunk(
        source_uri=f"{slug}/__hash_sentinel__",
        chunk_index=-1,
        raw_text="",
        token_count=0,
        source_type="oracle_skill_sentinel",
        partition_id=slug,
        embedding=None,
        topic_tags={"oracle_skill_hash": new_hash},
        is_deprecated=False,
    ))

    return len(chunks)


async def _run_reseed(db: "AsyncSession", root: Path | str | None = None) -> int:
    """Compare lockfile hashes against sentinels and reseed only changed partitions.

    Returns total chunks reseeded across all partitions (0 if none changed).
    """
    if root is None:
        # parents[4] from backend/app/workers/tasks/oracle_skill_reseed.py is the repo root
        root = Path(__file__).resolve().parents[4]
    root = Path(root)

    file_hashes = _read_lockfile_hashes(root)
    if not file_hashes:
        logger.warning("skills-lock.json missing or empty — skipping reseed")
        return 0

    partition_ids = list(SLUG_MAP.values())
    stored = await _read_stored_hashes(db, partition_ids)

    total_reseeded = 0
    for skill_name, slug in SLUG_MAP.items():
        new_hash = file_hashes.get(skill_name, "")
        old_hash = stored.get(slug, "")
        if new_hash and new_hash == old_hash:
            continue
        logger.info("Reseeding %s (hash %s → %s)", slug, old_hash[:8], new_hash[:8])
        total_reseeded += await _reseed_partition(db, root, skill_name, slug, new_hash)

    return total_reseeded


@celery_app.task(name="tasks.oracle_skill_reseed")
def reseed_oracle_skills_task() -> dict:
    """Celery entrypoint. Runs every 6 hours per Beat schedule."""
    async def _run():
        async with async_session_factory() as db:
            try:
                count = await _run_reseed(db)
                await db.commit()
                return {"status": "ok", "chunks_reseeded": count}
            except Exception as e:
                logger.exception("Oracle reseed task failed")
                await db.rollback()
                return {"status": "error", "error": str(e)}
    return asyncio.run(_run())
