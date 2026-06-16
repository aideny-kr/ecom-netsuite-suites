"""Backfill Celery task — distill existing tenant learning rows into memory concepts.

Reads the tenant's existing ``TenantLearnedRule`` (active) + ``TenantQueryPattern``
rows, asks the fast model (platform key — NOT BYOK) to distill them into reusable
plain-English *concepts* (see ``tenant_memory_extractor``), and persists each
concept (``review_state='pending'`` — it is NOT trusted until a human confirms it
in the self-serve graph) plus an evidence ``TenantMemoryLink`` back to the source
row.

Idempotency (re-runnable safely):
  * **Links** dedup on the ``uq_tenant_memory_link_source`` unique constraint
    (``tenant_id, source_table, source_id``) via ``on_conflict_do_update``.
  * **Concepts** dedup by normalized name — first against rows already in the DB
    (so a re-run reuses the prior concept), then within the current run.

RLS in the worker: the upsert loop batch-commits every 10 rows, and ``SET LOCAL``
is transaction-scoped (lost on each commit). So we set a *session-scoped* GUC
(plain ``SET``, persists across the batch commits) via ``_set_session_tenant``.

``tenant_id`` MUST be passed as a kwarg — ``InstrumentedTask`` reads
``kwargs['tenant_id']`` to scope the Job + audit rows.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.database import worker_async_session
from app.models.tenant_learned_rule import TenantLearnedRule
from app.models.tenant_memory_concept import TenantMemoryConcept
from app.models.tenant_memory_link import TenantMemoryLink
from app.models.tenant_query_pattern import TenantQueryPattern
from app.services.chat.tenant_memory_extractor import extract_concepts
from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

_COMMIT_EVERY = 10


def _normalize_name(name: str) -> str:
    """Case/whitespace-insensitive key for in-run + in-DB concept dedup."""
    return " ".join((name or "").strip().lower().split())


async def _set_session_tenant(db: AsyncSession, tenant_id: str) -> None:
    """Set the RLS tenant GUC *session-scoped* (plain SET, survives commits).

    The backfill batch-commits every ``_COMMIT_EVERY`` rows; a transaction-scoped
    ``SET LOCAL`` would be cleared after the first commit, so subsequent inserts
    would fail the RLS ``WITH CHECK``. A session-scoped ``SET`` persists for the
    life of the connection. ``SET`` does not accept bind params, so the UUID is
    validated (raises ``ValueError`` on bad input) before interpolation.
    """
    validated = str(uuid.UUID(str(tenant_id)))
    await db.execute(text(f"SET app.current_tenant_id = '{validated}'"))


async def _collect_source_rows(db: AsyncSession, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    """Gather active learned rules + query patterns as extractor source rows.

    Each row carries the natural-language ``text`` for the LLM plus the
    ``source_table`` / ``source_id`` evidence pointer used to mint the link.
    """
    rows: list[dict[str, Any]] = []

    rules = (
        (
            await db.execute(
                select(TenantLearnedRule).where(
                    TenantLearnedRule.tenant_id == tenant_id,
                    TenantLearnedRule.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for rule in rules:
        rows.append(
            {
                "kind": "learned_rule",
                "text": rule.rule_description,
                "category": rule.rule_category,
                "source_table": "tenant_learned_rules",
                "source_id": str(rule.id),
            }
        )

    patterns = (
        (await db.execute(select(TenantQueryPattern).where(TenantQueryPattern.tenant_id == tenant_id))).scalars().all()
    )
    for pattern in patterns:
        rows.append(
            {
                "kind": "query_pattern",
                "text": pattern.user_question,
                "source_table": "tenant_query_patterns",
                "source_id": str(pattern.id),
            }
        )

    return rows


async def _get_or_create_concept(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    concept: dict[str, Any],
    cache: dict[str, uuid.UUID],
) -> uuid.UUID:
    """Return the id of the concept for ``concept['name']``, minting if needed.

    Dedup order: in-run cache -> existing DB row (same normalized name) -> create.
    The created concept is ``review_state='pending'`` (untrusted until confirmed).
    """
    key = _normalize_name(concept.get("name", ""))
    if key in cache:
        return cache[key]

    existing = (
        (
            await db.execute(
                select(TenantMemoryConcept).where(
                    TenantMemoryConcept.tenant_id == tenant_id,
                    TenantMemoryConcept.name == concept["name"],
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        cache[key] = existing.id
        return existing.id

    confidence = concept.get("confidence")
    row = TenantMemoryConcept(
        tenant_id=tenant_id,
        name=concept["name"],
        summary=concept.get("plain_english_summary") or concept.get("summary") or "",
        concept_type=concept.get("concept_type"),
        review_state="pending",
        confidence=confidence if isinstance(confidence, (int, float)) else None,
    )
    db.add(row)
    await db.flush()
    cache[key] = row.id
    return row.id


async def _upsert_link(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    concept_id: uuid.UUID,
    source_table: str,
    source_id: uuid.UUID,
) -> None:
    """Idempotently point an evidence link at ``concept_id``.

    On conflict (same tenant/source row already linked) repoint to the freshly
    minted concept rather than erroring — keeps the backfill re-runnable.
    """
    stmt = pg_insert(TenantMemoryLink).values(
        tenant_id=tenant_id,
        concept_id=concept_id,
        source_table=source_table,
        source_id=source_id,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_tenant_memory_link_source",
        set_={"concept_id": concept_id},
    )
    await db.execute(stmt)


async def _extract(db: AsyncSession, tenant_id: str, job_id: uuid.UUID) -> dict[str, Any]:
    """Distill source rows into pending concepts + evidence links (idempotent).

    Returns a stats dict: ``rows_scanned``, ``concepts_extracted``,
    ``links_upserted``. Batch-commits every ``_COMMIT_EVERY`` upserts to stay
    under Supabase's 2-minute statement timeout.
    """
    tid = uuid.UUID(str(tenant_id))
    source_rows = await _collect_source_rows(db, tid)
    if not source_rows:
        return {"rows_scanned": 0, "concepts_extracted": 0, "links_upserted": 0}

    from app.services.chat.llm_adapter import get_adapter

    adapter = get_adapter(settings.MULTI_AGENT_SPECIALIST_PROVIDER, settings.ANTHROPIC_API_KEY)
    model = settings.MULTI_AGENT_SPECIALIST_MODEL

    concepts = await extract_concepts(source_rows, adapter, model)

    # Map normalized extracted-concept name -> the source rows it summarizes. A
    # concept the model didn't tie to a row still gets minted (no link).
    concept_cache: dict[str, uuid.UUID] = {}
    links_upserted = 0
    upserts_since_commit = 0

    for concept in concepts:
        name = concept.get("name")
        if not name:
            continue
        await _get_or_create_concept(db, tid, concept, concept_cache)

    # Link every source row to its best-matching concept. v1 mapping: a source row
    # links to the FIRST extracted concept (concepts are tenant-wide distillations,
    # not 1:1 with rows); the link constraint guarantees each source row appears once.
    primary_concept_id = next(iter(concept_cache.values()), None)
    if primary_concept_id is not None:
        for src in source_rows:
            await _upsert_link(
                db,
                tid,
                primary_concept_id,
                src["source_table"],
                uuid.UUID(src["source_id"]),
            )
            links_upserted += 1
            upserts_since_commit += 1
            if upserts_since_commit >= _COMMIT_EVERY:
                await db.commit()
                await _set_session_tenant(db, tenant_id)
                upserts_since_commit = 0

    await db.commit()

    logger.info(
        "tenant_memory_backfill.extracted",
        tenant_id=str(tid),
        job_id=str(job_id),
        rows_scanned=len(source_rows),
        concepts=len(concept_cache),
        links=links_upserted,
    )
    return {
        "rows_scanned": len(source_rows),
        "concepts_extracted": len(concept_cache),
        "links_upserted": links_upserted,
    }


@celery_app.task(
    base=InstrumentedTask,
    name="tasks.tenant_memory_extract_backfill",
    bind=True,
    queue="sync",
)
def tenant_memory_extract_backfill_task(self, tenant_id: str, **kwargs) -> dict[str, Any]:
    """Backfill the tenant memory graph from existing learning rows.

    ``tenant_id`` is a required kwarg (InstrumentedTask scopes Job + audit by it).
    """

    async def _run() -> dict[str, Any]:
        async with worker_async_session() as db:
            await _set_session_tenant(db, tenant_id)
            return await _extract(db, tenant_id, self._job_id or uuid.uuid4())

    return asyncio.run(_run())
