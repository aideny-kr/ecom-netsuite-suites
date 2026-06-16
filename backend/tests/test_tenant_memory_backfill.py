"""Tests for the tenant-memory backfill Celery task + admin trigger endpoint.

Covers:
  (a) ``_extract`` is idempotent — a second run over the same source rows mints
      no new concept and no new link rows (the link unique constraint
      ``uq_tenant_memory_link_source`` + in-DB concept-name dedup are the keys).
  (b) ``POST /tenant-memory/backfill`` requires ``tenant.manage`` (member -> 403)
      and dispatches ``tasks.tenant_memory_extract_backfill`` via ``send_task``
      with ``kwargs={"tenant_id": <str>}`` on the ``sync`` queue.

DB tests require a live local Postgres (dangerouslyDisableSandbox).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.tenant_learned_rule import TenantLearnedRule
from app.models.tenant_memory_concept import TenantMemoryConcept
from app.models.tenant_memory_link import TenantMemoryLink
from app.models.tenant_query_pattern import TenantQueryPattern

_FAKE_CONCEPTS = [
    {
        "name": "Net Revenue",
        "concept_type": "definition",
        "plain_english_summary": "Revenue excluding refunds.",
        "edges": [],
        "confidence": 0.9,
    },
    {
        "name": "Failed Order",
        "concept_type": "definition",
        "plain_english_summary": "An order whose status is failed.",
        "edges": [],
        "confidence": 0.8,
    },
]


async def _seed_sources(db, tenant_id):
    rule = TenantLearnedRule(
        tenant_id=tenant_id,
        rule_category="term_definition",
        rule_description="net revenue excludes refunds",
        is_active=True,
    )
    inactive = TenantLearnedRule(
        tenant_id=tenant_id,
        rule_category="term_definition",
        rule_description="ignored — inactive",
        is_active=False,
    )
    pattern = TenantQueryPattern(
        tenant_id=tenant_id,
        user_question="how many failed orders",
        working_sql="SELECT 1",
    )
    db.add_all([rule, inactive, pattern])
    await db.flush()
    return rule, inactive, pattern


async def test_extract_is_idempotent(db, admin_user):
    """A second _extract call mints no new concept/link rows (idempotent)."""
    from app.workers.tasks import tenant_memory_extract_backfill as bf

    user, _ = admin_user
    tenant_id = str(user.tenant_id)
    await _seed_sources(db, user.tenant_id)

    job_id = uuid.uuid4()
    # extract_concepts maps one batch of source rows -> the same two concepts both
    # runs, so dedup-by-name + the link unique constraint must absorb the rerun.
    with patch.object(bf, "extract_concepts", new=AsyncMock(return_value=_FAKE_CONCEPTS)):
        stats1 = await bf._extract(db, tenant_id, job_id)
        await db.flush()

        links_after_first = (
            (await db.execute(select(TenantMemoryLink).where(TenantMemoryLink.tenant_id == user.tenant_id)))
            .scalars()
            .all()
        )
        concepts_after_first = (
            (await db.execute(select(TenantMemoryConcept).where(TenantMemoryConcept.tenant_id == user.tenant_id)))
            .scalars()
            .all()
        )

        # One active learned rule + one query pattern -> exactly two links.
        assert len(links_after_first) == 2
        assert stats1["links_upserted"] == 2
        assert {link.source_table for link in links_after_first} == {
            "tenant_learned_rules",
            "tenant_query_patterns",
        }
        first_concept_count = len(concepts_after_first)
        assert first_concept_count >= 1
        assert all(c.review_state == "pending" for c in concepts_after_first)

        # Second run — must NOT create new rows.
        await bf._extract(db, tenant_id, job_id)
        await db.flush()

        links_after_second = (
            (await db.execute(select(TenantMemoryLink).where(TenantMemoryLink.tenant_id == user.tenant_id)))
            .scalars()
            .all()
        )
        concepts_after_second = (
            (await db.execute(select(TenantMemoryConcept).where(TenantMemoryConcept.tenant_id == user.tenant_id)))
            .scalars()
            .all()
        )

    assert len(links_after_second) == 2
    assert len(concepts_after_second) == first_concept_count


async def test_extract_dedups_concept_across_casing(db, admin_user):
    """Cross-run dedup is normalized: a re-run that yields the same concept with
    different casing/whitespace reuses the existing concept (no duplicate)."""
    from app.workers.tasks import tenant_memory_extract_backfill as bf

    user, _ = admin_user
    tenant_id = str(user.tenant_id)
    await _seed_sources(db, user.tenant_id)

    run1 = [
        {
            "name": "Net Revenue",
            "concept_type": "definition",
            "plain_english_summary": "Revenue excluding refunds.",
            "edges": [],
            "confidence": 0.9,
        }
    ]
    # Same concept, different casing + collapsible whitespace -> same normalized key.
    run2 = [
        {
            "name": "  net   revenue ",
            "concept_type": "definition",
            "plain_english_summary": "Revenue excluding refunds.",
            "edges": [],
            "confidence": 0.9,
        }
    ]

    with patch.object(bf, "extract_concepts", new=AsyncMock(return_value=run1)):
        await bf._extract(db, tenant_id, uuid.uuid4())
        await db.flush()
    count1 = len(
        (await db.execute(select(TenantMemoryConcept).where(TenantMemoryConcept.tenant_id == user.tenant_id)))
        .scalars()
        .all()
    )

    with patch.object(bf, "extract_concepts", new=AsyncMock(return_value=run2)):
        await bf._extract(db, tenant_id, uuid.uuid4())
        await db.flush()
    count2 = len(
        (await db.execute(select(TenantMemoryConcept).where(TenantMemoryConcept.tenant_id == user.tenant_id)))
        .scalars()
        .all()
    )

    assert count2 == count1, "casing/whitespace variant should reuse the existing concept, not mint a duplicate"


async def test_extract_clamps_out_of_range_confidence(db, admin_user):
    """A hallucinated confidence (50) must be clamped to [0,1], not overflow the
    Numeric(4,3) column and abort the whole backfill transaction."""
    from app.workers.tasks import tenant_memory_extract_backfill as bf

    user, _ = admin_user
    tenant_id = str(user.tenant_id)
    await _seed_sources(db, user.tenant_id)

    overflow_concepts = [
        {
            "name": "Net Revenue",
            "concept_type": "definition",
            "plain_english_summary": "Revenue excluding refunds.",
            "edges": [],
            "confidence": 50,  # hallucinated — would overflow Numeric(4,3)
        }
    ]
    with patch.object(bf, "extract_concepts", new=AsyncMock(return_value=overflow_concepts)):
        # Must NOT raise.
        await bf._extract(db, tenant_id, uuid.uuid4())
        await db.flush()

    row = (
        await db.execute(
            select(TenantMemoryConcept).where(
                TenantMemoryConcept.tenant_id == user.tenant_id,
                TenantMemoryConcept.name == "Net Revenue",
            )
        )
    ).scalar_one()
    assert float(row.confidence) == 1.0, "out-of-range confidence must clamp to 1.0"


async def test_extract_truncates_overlong_name_and_type(db, admin_user):
    """An over-length name (300 chars) / concept_type must be truncated to the
    column limits (255 / 50), not overflow and abort the backfill."""
    from app.workers.tasks import tenant_memory_extract_backfill as bf

    user, _ = admin_user
    tenant_id = str(user.tenant_id)
    await _seed_sources(db, user.tenant_id)

    long_name = "X" * 300
    long_type = "definition" * 10  # 100 chars > String(50)
    overlong_concepts = [
        {
            "name": long_name,
            "concept_type": long_type,
            "plain_english_summary": "Revenue excluding refunds.",
            "edges": [],
            "confidence": 0.9,
        }
    ]
    with patch.object(bf, "extract_concepts", new=AsyncMock(return_value=overlong_concepts)):
        # Must NOT raise.
        await bf._extract(db, tenant_id, uuid.uuid4())
        await db.flush()

    row = (
        await db.execute(
            select(TenantMemoryConcept).where(
                TenantMemoryConcept.tenant_id == user.tenant_id,
                TenantMemoryConcept.name == long_name[:255],
            )
        )
    ).scalar_one()
    assert len(row.name) == 255
    assert len(row.concept_type) == 50


async def test_extract_skips_when_no_sources(db, admin_user):
    """With no source rows the extractor is a no-op (no concepts, no links)."""
    from app.workers.tasks import tenant_memory_extract_backfill as bf

    user, _ = admin_user
    with patch.object(bf, "extract_concepts", new=AsyncMock(return_value=[])):
        stats = await bf._extract(db, str(user.tenant_id), uuid.uuid4())
    assert stats["links_upserted"] == 0
    links = (
        (await db.execute(select(TenantMemoryLink).where(TenantMemoryLink.tenant_id == user.tenant_id))).scalars().all()
    )
    assert links == []


async def test_trigger_requires_permission(client, member_user):
    """A non-admin (no tenant.manage) cannot trigger the backfill."""
    _, headers = member_user
    resp = await client.post("/api/v1/tenant-memory/backfill", headers=headers)
    assert resp.status_code == 403, resp.text


async def test_trigger_dispatches_with_tenant_kwarg(client, admin_user):
    """Admin trigger calls send_task with kwargs tenant_id on the sync queue."""
    user, headers = admin_user

    mock_result = MagicMock()
    mock_result.id = "celery-task-xyz"
    with patch("app.api.v1.tenant_memory.celery_app") as mock_celery:
        mock_celery.send_task.return_value = mock_result
        resp = await client.post("/api/v1/tenant-memory/backfill", headers=headers)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["celery_task_id"] == "celery-task-xyz"
    assert body["status"] == "queued"

    mock_celery.send_task.assert_called_once()
    call = mock_celery.send_task.call_args
    assert call.args[0] == "tasks.tenant_memory_extract_backfill"
    assert call.kwargs["kwargs"] == {"tenant_id": str(user.tenant_id)}
    assert call.kwargs["queue"] == "sync"


def test_task_registered_with_instrumented_base():
    """The task is importable, registered, sync-queued, and InstrumentedTask-based."""
    from app.workers.base_task import InstrumentedTask
    from app.workers.celery_app import celery_app
    from app.workers.tasks.tenant_memory_extract_backfill import (
        tenant_memory_extract_backfill_task,
    )

    assert "tasks.tenant_memory_extract_backfill" in celery_app.tasks
    assert isinstance(tenant_memory_extract_backfill_task, InstrumentedTask)
    assert tenant_memory_extract_backfill_task.queue == "sync"
    assert "app.workers.tasks.tenant_memory_extract_backfill" in celery_app.conf.include


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
