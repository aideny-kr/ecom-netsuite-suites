# backend/tests/services/metrics/test_metric_seed_commit_idempotency.py
"""F5 — seed→re-seed idempotency ACROSS A REAL COMMIT BOUNDARY.

The existing ``test_metric_catalog_seeder.test_seed_is_idempotent_and_system_scoped``
calls the seeder twice inside the *same* rolled-back transaction (the shared ``db``
fixture wraps every test in an outer transaction that is never committed). Both
seeder calls therefore see each other's *uncommitted* DELETE+INSERT, so it can
only prove within-transaction idempotency. It does NOT exercise the real-world
F5 path the post-flight flagged: the catalog is seeded and **COMMITTED** (e.g. by
``python -m app.scripts.seed_metric_catalog`` or migration 080), and the seeder
runs **again** against that committed state.

The seeder guarantees cross-commit idempotency via its leading
``DELETE FROM metric_definitions WHERE tenant_id = SYSTEM_TENANT_ID`` — without
that DELETE a second committed run would either double the rows (2N) or, given
``UNIQUE(tenant_id, key)``, raise a UniqueViolationError. This test asserts that
true invariant: after two committed seed passes the catalog holds EXACTLY N
SYSTEM rows (one per key), each with the seeded key, and no duplicates.

It deliberately bypasses the rollback ``db`` fixture and uses its own committing
engine. A finally-block restores the canonical committed seed so the shared local
DB is left in the same seeded baseline the F5 task established (no test pollution).
"""

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.services.metrics.metric_catalog_seeder import _SYSTEM_METRICS, seed_system_metrics

pytestmark = pytest.mark.asyncio

_db_url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL


async def _count_system_rows(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(MetricDefinition).where(MetricDefinition.tenant_id == SYSTEM_TENANT_ID)
        )
    ).scalar_one()


async def _system_keys(session: AsyncSession) -> list[str]:
    return list(
        (await session.execute(select(MetricDefinition.key).where(MetricDefinition.tenant_id == SYSTEM_TENANT_ID)))
        .scalars()
        .all()
    )


async def test_seed_is_idempotent_across_commit_boundary():
    """Two COMMITTED seed passes leave exactly N SYSTEM rows — no doubling, no
    UNIQUE(tenant_id, key) violation. This is the cross-commit invariant the
    within-transaction test cannot reach."""
    if "supabase" in _db_url:  # safety: never run a committing test against remote
        pytest.skip("cross-commit seed idempotency test runs against LOCAL docker only")

    n_expected = len(_SYSTEM_METRICS)
    engine = create_async_engine(_db_url, echo=False)
    try:
        # Pass 1: clean slate → seed → COMMIT (mimics a fresh `seed_metric_catalog`).
        async with AsyncSession(engine, expire_on_commit=False) as s:
            await s.execute(delete(MetricDefinition).where(MetricDefinition.tenant_id == SYSTEM_TENANT_ID))
            n1 = await seed_system_metrics(s)
            await s.commit()

        # Independent session reads the COMMITTED state from pass 1.
        async with AsyncSession(engine, expire_on_commit=False) as s:
            assert await _count_system_rows(s) == n1 == n_expected

        # Pass 2: re-seed against the ALREADY-COMMITTED catalog → COMMIT.
        # Without the seeder's leading DELETE this raises UniqueViolationError
        # (or, if the DELETE were scoped wrong, leaves 2N rows).
        async with AsyncSession(engine, expire_on_commit=False) as s:
            n2 = await seed_system_metrics(s)
            await s.commit()

        # The real invariant: still exactly N rows, one per key, no duplicates.
        async with AsyncSession(engine, expire_on_commit=False) as s:
            total = await _count_system_rows(s)
            keys = await _system_keys(s)
            assert n2 == n_expected
            assert total == n_expected, f"re-seed across a commit produced {total} rows, expected {n_expected}"
            assert len(set(keys)) == len(keys), f"duplicate SYSTEM keys after re-seed: {keys}"
            assert set(keys) == {m["key"] for m in _SYSTEM_METRICS}
    finally:
        # Restore the canonical committed seed baseline (leave the shared DB seeded).
        async with AsyncSession(engine, expire_on_commit=False) as s:
            await seed_system_metrics(s)
            await s.commit()
        await engine.dispose()
