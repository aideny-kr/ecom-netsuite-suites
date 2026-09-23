"""A worker session's tenant context holds for every transaction it runs.

set_tenant_context_session used a plain session-level SET, which fails in two ways:
- a rollback undoes the SET when nothing has committed since it ran, so a task whose
  first write fails continues with no tenant context;
- a connection the pool replaces mid-run (pool_recycle, failed pre-ping) starts without
  it, and a pooled connection handed on still carries the old tenant.
Found by codex's review of #285 (2026-09-22). The context is now applied at the start
of EVERY transaction, as SET LOCAL, so it can be neither lost nor leaked.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.database import set_tenant_context_session
from tests.conftest import _test_connect_args, _test_db_url

_CURRENT = text("SELECT current_setting('app.current_tenant_id', true)")


async def _current(session) -> str | None:
    return (await session.execute(_CURRENT)).scalar() or None


async def test_a_rollback_before_any_commit_keeps_the_context(db):
    tenant = str(uuid.uuid4())
    await set_tenant_context_session(db, tenant)
    await db.rollback()
    assert await _current(db) == tenant


async def test_the_context_survives_a_commit(db):
    tenant = str(uuid.uuid4())
    await set_tenant_context_session(db, tenant)
    await db.commit()
    assert await _current(db) == tenant


@pytest.fixture
async def one_connection_engine():
    engine = create_async_engine(_test_db_url, connect_args=_test_connect_args, pool_size=1, max_overflow=0)
    yield engine
    await engine.dispose()


async def test_a_replaced_connection_gets_the_context(one_connection_engine):
    """pool_recycle or a failed pre-ping hands the session a new physical connection."""
    tenant = str(uuid.uuid4())
    async with AsyncSession(one_connection_engine, expire_on_commit=False) as session:
        await set_tenant_context_session(session, tenant)
        first_pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar()
        await session.commit()
        await (await session.connection()).invalidate()  # the physical connection is gone
        await session.rollback()
        assert (await session.execute(text("SELECT pg_backend_pid()"))).scalar() != first_pid
        assert await _current(session) == tenant


async def test_a_pooled_connection_carries_no_tenant_to_its_next_user(one_connection_engine):
    tenant = str(uuid.uuid4())
    async with AsyncSession(one_connection_engine, expire_on_commit=False) as session:
        await set_tenant_context_session(session, tenant)
        await session.commit()
        await _current(session)  # leave the connection used with the context set
        await session.commit()
    async with AsyncSession(one_connection_engine, expire_on_commit=False) as next_user:
        assert await _current(next_user) is None


async def test_calling_it_again_switches_tenant_without_stacking_listeners(db):
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    await set_tenant_context_session(db, first)
    await set_tenant_context_session(db, second)
    await db.rollback()
    assert await _current(db) == second


def test_an_invalid_tenant_id_is_refused():
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(set_tenant_context_session(None, "not-a-uuid"))
