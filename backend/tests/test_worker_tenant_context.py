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


@pytest.fixture
async def one_connection_engine():
    """Real transactions on a real engine. The shared ``db`` fixture turns a commit into
    RELEASE SAVEPOINT, which does not clear SET LOCAL, so a GUC assertion across a
    commit there would pass with no fix at all (see tests/conftest.py)."""
    engine = create_async_engine(_test_db_url, connect_args=_test_connect_args, pool_size=1, max_overflow=0)
    yield engine
    await engine.dispose()


async def test_a_rollback_before_any_commit_keeps_the_context(one_connection_engine):
    tenant = str(uuid.uuid4())
    async with AsyncSession(one_connection_engine, expire_on_commit=False) as session:
        await set_tenant_context_session(session, tenant)
        await session.rollback()
        assert await _current(session) == tenant


async def test_the_context_survives_real_commits(one_connection_engine):
    tenant = str(uuid.uuid4())
    async with AsyncSession(one_connection_engine, expire_on_commit=False) as session:
        await set_tenant_context_session(session, tenant)
        await session.commit()
        assert await _current(session) == tenant
        await session.commit()
        assert await _current(session) == tenant


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


def _count_tenant_sets(engine) -> list:
    from sqlalchemy import event

    statements = []

    def record(conn, cursor, statement, *args):
        if "set_config('app.current_tenant_id'" in statement:
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    return statements


async def test_a_session_is_scoped_to_one_tenant(one_connection_engine):
    """Switching tenant on a session is refused: a switch inside a savepoint that later
    rolls back would leave the recorded tenant and the real GUC disagreeing. The same
    tenant again is a re-apply, and the listener is registered once."""
    from app.core.database import set_tenant_context

    statements = _count_tenant_sets(one_connection_engine)
    tenant = str(uuid.uuid4())
    async with AsyncSession(one_connection_engine, expire_on_commit=False) as session:
        await set_tenant_context_session(session, tenant)
        await set_tenant_context_session(session, tenant)
        with pytest.raises(ValueError, match="one tenant"):
            await set_tenant_context_session(session, str(uuid.uuid4()))
        with pytest.raises(ValueError, match="one tenant"):
            await set_tenant_context(session, str(uuid.uuid4()))
        await session.commit()
        statements.clear()
        assert await _current(session) == tenant  # a new transaction
        assert len(statements) == 1


async def test_a_logically_begun_transaction_sets_the_tenant_once(one_connection_engine):
    """session.add() autobegins a transaction without a connection: in_transaction() is
    true before the physical BEGIN that fires the listener."""
    from app.models.job import Job

    statements = _count_tenant_sets(one_connection_engine)
    tenant = str(uuid.uuid4())
    async with AsyncSession(one_connection_engine, expire_on_commit=False) as session:
        session.add(Job(tenant_id=uuid.UUID(tenant), job_type="probe", status="pending"))
        assert session.in_transaction()
        await set_tenant_context_session(session, tenant)
        assert len(statements) == 1
        assert await _current(session) == tenant
        await session.rollback()


async def test_a_fresh_session_sets_the_tenant_once(one_connection_engine):
    """The first call on a session with no transaction must not issue the SET twice
    (once from the listener on begin, once explicitly)."""
    statements = _count_tenant_sets(one_connection_engine)
    async with AsyncSession(one_connection_engine, expire_on_commit=False) as session:
        await set_tenant_context_session(session, str(uuid.uuid4()))
        assert len(statements) == 1


def test_an_invalid_tenant_id_is_refused():
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(set_tenant_context_session(None, "not-a-uuid"))
