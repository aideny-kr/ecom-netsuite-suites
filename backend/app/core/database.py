import ssl
import uuid
from collections.abc import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings


def _is_supabase(url: str) -> bool:
    """Check if the database URL points to Supabase."""
    return "supabase.com" in url or "supabase.co" in url


def _build_connect_args(url: str) -> dict:
    """Build connection args with SSL for Supabase, plain for local."""
    if _is_supabase(url):
        ssl_ctx = ssl.create_default_context()
        # Supabase uses a self-signed cert in the chain that slim Docker images
        # don't trust. Disable verification (connection is still encrypted).
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        return {"ssl": ssl_ctx}
    return {}


# Prefer direct connection (bypasses PgBouncer) when available
_db_url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
_is_remote = _is_supabase(_db_url)
_pool_size = 20 if _is_remote else 20
_max_overflow = 30 if _is_remote else 10

engine = create_async_engine(
    _db_url,
    echo=settings.APP_DEBUG,
    pool_size=_pool_size,
    max_overflow=_max_overflow,
    connect_args=_build_connect_args(_db_url),
    pool_pre_ping=True,
    pool_recycle=300 if _is_remote else -1,
)

async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def set_tenant_context(session: AsyncSession, tenant_id: str) -> None:
    """Set RLS tenant context for the current database session.

    PostgreSQL SET LOCAL does not support parameterized queries ($1 binds),
    so we validate the tenant_id is a valid UUID to prevent SQL injection.
    """
    validated = str(uuid.UUID(str(tenant_id)))  # Raises ValueError if not a valid UUID
    await session.execute(text(f"SET LOCAL app.current_tenant_id = '{validated}'"))


_TENANT_CONTEXT_KEY = "tenant_context"
# set_config(..., is_local => true) is SET LOCAL that accepts a bind parameter.
_SET_TENANT_LOCAL = text("SELECT set_config('app.current_tenant_id', :tenant_id, true)")


def _apply_tenant_context(session, transaction, connection) -> None:
    """after_begin: every transaction the session starts runs under its tenant."""
    tenant_id = session.info.get(_TENANT_CONTEXT_KEY)
    if tenant_id:
        connection.execute(_SET_TENANT_LOCAL, {"tenant_id": tenant_id})


async def set_tenant_context_session(session: AsyncSession, tenant_id: str) -> None:
    """Tenant context for EVERY transaction this session runs, for worker tasks whose
    services commit (and roll back) mid-run.

    No single SET can hold across such a run. SET LOCAL ends at the first commit. A
    plain SET is undone by a rollback that follows it before any commit, is missing on
    a connection the pool replaces mid-run (pool_recycle, failed pre-ping), and stays
    on a pooled connection handed to the next user. So the tenant is recorded on the
    session and applied as SET LOCAL at the start of each transaction by an after_begin
    listener, and once now for the transaction in progress. Calling it again switches
    the tenant; the listener is registered once per session.
    """
    validated = str(uuid.UUID(str(tenant_id)))  # Raises ValueError if not a valid UUID
    sync_session = session.sync_session
    if _TENANT_CONTEXT_KEY not in sync_session.info:
        event.listen(sync_session, "after_begin", _apply_tenant_context)
    sync_session.info[_TENANT_CONTEXT_KEY] = validated
    await session.execute(_SET_TENANT_LOCAL, {"tenant_id": validated})


def worker_async_session():
    """Create a fresh async engine + session for Celery worker tasks.

    Each Celery prefork worker creates its own event loop via asyncio.new_event_loop().
    The module-level engine/session_factory are bound to the main process's loop and
    cannot be reused. This function creates a disposable engine per task invocation.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session():
        _engine = create_async_engine(
            _db_url,
            echo=settings.APP_DEBUG,
            pool_size=2,
            max_overflow=3,
            connect_args=_build_connect_args(_db_url),
            pool_pre_ping=True,
            pool_recycle=300 if _is_remote else -1,
        )
        factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            try:
                yield session
            finally:
                await session.close()
        await _engine.dispose()

    return _session()
