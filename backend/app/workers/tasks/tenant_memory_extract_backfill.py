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
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.database import worker_async_session
from app.models.tenant_learned_rule import TenantLearnedRule
from app.models.tenant_memory_concept import TenantMemoryConcept
from app.models.tenant_memory_edge import TenantMemoryEdge
from app.models.tenant_memory_link import TenantMemoryLink
from app.models.tenant_query_pattern import TenantQueryPattern
from app.services.chat.tenant_memory_extractor import extract_concepts
from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

_COMMIT_EVERY = 10
# Cap the single-prompt distillation input so a large tenant can't blow the LLM
# context window (the rows are sent in one {{ROWS}} prompt). Excess is dropped + logged.
_MAX_SOURCE_ROWS = 300


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

    if len(rows) > _MAX_SOURCE_ROWS:
        logger.warning(
            "tenant_memory_backfill.source_rows_capped",
            tenant_id=str(tenant_id),
            total=len(rows),
            cap=_MAX_SOURCE_ROWS,
        )
        rows = rows[:_MAX_SOURCE_ROWS]

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

    # Match on the SAME normalization as the in-run cache key (_normalize_name:
    # lower + collapse/trim whitespace) so a re-run with different casing/spacing
    # reuses the existing concept instead of minting a duplicate.
    existing = (
        (
            await db.execute(
                select(TenantMemoryConcept).where(
                    TenantMemoryConcept.tenant_id == tenant_id,
                    func.trim(func.regexp_replace(func.lower(TenantMemoryConcept.name), r"\s+", " ", "g")) == key,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        cache[key] = existing.id
        return existing.id

    # Clamp the LLM confidence to [0,1]. The column is Numeric(4,3) (abs < 10);
    # a hallucinated 10.0+ overflows and aborts the whole backfill transaction.
    raw_confidence = concept.get("confidence")
    # bool is a subclass of int — exclude it so a stray JSON `true` clamps to None, not 1.0.
    confidence = (
        max(0.0, min(1.0, float(raw_confidence)))
        if isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool)
        else None
    )
    # Truncate to the column limits (name String(255), concept_type String(50)) —
    # raw LLM output of any length would otherwise overflow and abort the backfill.
    name = concept["name"]
    name = name[:255] if name is not None else name
    concept_type = concept.get("concept_type")
    concept_type = concept_type[:50] if concept_type is not None else concept_type
    # Insert with ON CONFLICT DO NOTHING on the (tenant, normalized-name) unique
    # index (uq_tmc_tenant_norm_name) so two concurrent backfills converge to ONE
    # row instead of both inserting (the loser would otherwise raise an
    # IntegrityError and abort the run). If our insert lost the race, re-select the
    # winner's id.
    summary = concept.get("plain_english_summary") or concept.get("summary") or ""
    insert_stmt = (
        pg_insert(TenantMemoryConcept)
        .values(
            tenant_id=tenant_id,
            name=name,
            summary=summary,
            concept_type=concept_type,
            review_state="pending",
            confidence=confidence,
        )
        .on_conflict_do_nothing()
        .returning(TenantMemoryConcept.id)
    )
    new_id = (await db.execute(insert_stmt)).scalar_one_or_none()
    if new_id is None:
        new_id = (
            await db.execute(
                select(TenantMemoryConcept.id).where(
                    TenantMemoryConcept.tenant_id == tenant_id,
                    func.trim(func.regexp_replace(func.lower(TenantMemoryConcept.name), r"\s+", " ", "g")) == key,
                )
            )
        ).scalar_one()
    cache[key] = new_id
    return new_id


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


async def _upsert_edge(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    source_concept_id: uuid.UUID,
    target_concept_id: uuid.UUID,
    relation: str,
) -> None:
    """Idempotently insert a pending relationship edge between two concepts.

    On conflict (same tenant/source/target/relation already present) do nothing,
    so a re-run mints no duplicate edge.
    """
    stmt = pg_insert(TenantMemoryEdge).values(
        tenant_id=tenant_id,
        source_concept_id=source_concept_id,
        target_concept_id=target_concept_id,
        relation=relation[:100],
        review_state="pending",
    )
    stmt = stmt.on_conflict_do_nothing(constraint="uq_tenant_memory_edge")
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

    # Persist the extracted relationships as pending edges. Resolve each edge's
    # target name to a concept id via the same normalized-name cache; skip edges
    # whose target isn't a known concept. Idempotent on uq_tenant_memory_edge.
    for concept in concepts:
        source_key = _normalize_name(concept.get("name", ""))
        source_id = concept_cache.get(source_key)
        if source_id is None:
            continue
        for edge in concept.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            target_key = _normalize_name(edge.get("target", ""))
            relation = edge.get("relation")
            target_id = concept_cache.get(target_key)
            if not target_key or target_id is None or not relation:
                continue
            await _upsert_edge(db, tid, source_id, target_id, str(relation))

    # Attribute each source row to the concept that lists its source_id. Build a
    # source_id -> concept_id map from the concepts' reported source_ids; a row the
    # model didn't attribute falls back to the FIRST concept so every source row
    # still gets exactly one link (uq_tenant_memory_link_source guarantees one per
    # row). v1: concepts are tenant-wide distillations, not strictly 1:1 with rows.
    primary_concept_id = next(iter(concept_cache.values()), None)
    source_to_concept: dict[str, uuid.UUID] = {}
    for concept in concepts:
        key = _normalize_name(concept.get("name", ""))
        cid = concept_cache.get(key)
        if cid is None:
            continue
        for sid in concept.get("source_ids") or []:
            # First concept to claim a source_id wins (deterministic, stable order).
            source_to_concept.setdefault(str(sid), cid)

    if primary_concept_id is not None:
        for src in source_rows:
            concept_id = source_to_concept.get(src["source_id"], primary_concept_id)
            await _upsert_link(
                db,
                tid,
                concept_id,
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
