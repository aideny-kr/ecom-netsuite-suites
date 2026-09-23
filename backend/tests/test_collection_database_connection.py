"""Real transaction boundaries for a task-owned connection; no savepoint fixture."""

from uuid import uuid4

import pytest
from sqlalchemy import event, text

from app.core import database as mod
from tests.conftest import _test_db_url


@pytest.fixture
def engines(monkeypatch):
    created, checkouts = [], []
    original = mod.create_async_engine

    def create(*args, **kwargs):
        engine = original(*args, **kwargs)
        created.append(engine)
        event.listen(engine.sync_engine, "checkout", lambda *args: checkouts.append(True))
        return engine

    monkeypatch.setattr(mod, "_db_url", _test_db_url)
    monkeypatch.setattr(mod, "create_async_engine", create)
    return created, checkouts


async def test_pinned_connection_has_real_commits_and_local_tenant_boundaries(engines):
    tenant = str(uuid4())
    async with mod.worker_async_session(pin_connection=True) as db:
        await db.execute(text("CREATE TEMP TABLE collection_commit_probe (id int) ON COMMIT DELETE ROWS"))
        await db.commit()
        for _ in range(3):
            await mod.set_tenant_context(db, tenant)
            assert await db.scalar(text("SELECT current_setting('app.current_tenant_id', true)")) == tenant
            await db.execute(text("INSERT INTO collection_commit_probe VALUES (1)"))
            assert await db.scalar(text("SELECT count(*) FROM collection_commit_probe")) == 1
            await db.commit()
            assert await db.scalar(text("SELECT count(*) FROM collection_commit_probe")) == 0
            assert not await db.scalar(text("SELECT current_setting('app.current_tenant_id', true)"))
            await db.rollback()
        assert len(engines[1]) == 1
    assert engines[0][0].pool.checkedout() == 0


@pytest.mark.parametrize("pin", [False, True])
async def test_task_failure_closes_and_disposes_its_connection(engines, pin):
    with pytest.raises(RuntimeError, match="task failed"):
        async with mod.worker_async_session(pin_connection=pin) as db:
            await db.execute(text("SELECT 1"))
            raise RuntimeError("task failed")
    assert engines[0][0].pool.checkedout() == 0
    assert engines[0][0].pool.checkedin() == 0


async def test_invalidated_pinned_connection_recovers_after_rollback(engines):
    tenant = str(uuid4())
    async with mod.worker_async_session(pin_connection=True) as db:
        await mod.set_tenant_context(db, tenant)
        first = await db.scalar(text("SELECT pg_backend_pid()"))
        await (await db.connection()).invalidate()
        await db.rollback()
        await mod.set_tenant_context(db, tenant)
        assert await db.scalar(text("SELECT pg_backend_pid()")) != first
        assert await db.scalar(text("SELECT current_setting('app.current_tenant_id', true)")) == tenant


async def test_cancelled_collection_releases_the_connection(engines):
    import asyncio

    with pytest.raises(asyncio.CancelledError):
        async with mod.worker_async_session(pin_connection=True) as db:
            await db.execute(text("SELECT 1"))
            raise asyncio.CancelledError
    assert engines[0][0].pool.checkedout() == 0
    assert engines[0][0].pool.checkedin() == 0
