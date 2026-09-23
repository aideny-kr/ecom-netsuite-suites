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


# set_config(..., is_local => true) is SET LOCAL that accepts a bind parameter.
_SET_TENANT_LOCAL = text("SELECT set_config('app.current_tenant_id', :tenant_id, true)")
_TENANT_CONTEXT_KEY = "tenant_context"
_TENANT_APPLIED_KEY = "tenant_context_applied"


def _refuse_another_tenant(session: AsyncSession, tenant_id: str) -> None:
    # Only a session that recorded a tenant can refuse another; anything else (a
    # session never scoped, a test double) has nothing to compare against.
    info = getattr(getattr(session, "sync_session", None), "info", None)
    scoped = info.get(_TENANT_CONTEXT_KEY) if isinstance(info, dict) else None
    if isinstance(scoped, str) and scoped != tenant_id:
        raise ValueError("a worker session is scoped to one tenant; open another session for another tenant")


async def set_tenant_context(session: AsyncSession, tenant_id: str) -> None:
    """Set RLS tenant context for the current database session.

    PostgreSQL SET LOCAL does not support parameterized queries ($1 binds),
    so we validate the tenant_id is a valid UUID to prevent SQL injection.
    On a session scoped by set_tenant_context_session, only that same tenant is allowed.
    """
    validated = str(uuid.UUID(str(tenant_id)))  # Raises ValueError if not a valid UUID
    _refuse_another_tenant(session, validated)
    await session.execute(text(f"SET LOCAL app.current_tenant_id = '{validated}'"))


def _apply_tenant_context(session, transaction, connection) -> None:
    """after_begin: every transaction the session starts runs under its tenant."""
    tenant_id = session.info.get(_TENANT_CONTEXT_KEY)
    if tenant_id:
        connection.execute(_SET_TENANT_LOCAL, {"tenant_id": tenant_id})
        session.info[_TENANT_APPLIED_KEY] = True


async def set_tenant_context_session(session: AsyncSession, tenant_id: str) -> None:
    """Tenant context for EVERY transaction this session runs, for worker tasks whose
    services commit (and roll back) mid-run.

    No single SET can hold across such a run. SET LOCAL ends at the first commit. A
    plain SET is undone by a rollback that follows it before any commit, is missing on
    a connection the pool replaces mid-run (pool_recycle, failed pre-ping), and stays
    on a pooled connection handed to the next user. So the tenant is recorded on the
    session and applied as SET LOCAL at the start of each transaction by an after_begin
    listener, registered once per session.

    A session is scoped to ONE tenant, set outside any savepoint: the same tenant again
    re-applies it, another tenant raises, and a call inside a savepoint raises. A tenant
    set inside a savepoint that later rolled back would leave the recorded tenant and
    the real GUC disagreeing, so neither switching nor savepoint scoping is offered.
    """
    validated = str(uuid.UUID(str(tenant_id)))  # Raises ValueError if not a valid UUID
    _refuse_another_tenant(session, validated)
    sync_session = session.sync_session
    if sync_session.in_nested_transaction():
        # Applied inside a savepoint, the tenant would be reverted by that savepoint's
        # rollback while the session still recorded it.
        raise ValueError("set the worker session's tenant before opening a savepoint")
    if _TENANT_CONTEXT_KEY not in sync_session.info:
        event.listen(sync_session, "after_begin", _apply_tenant_context)
    sync_session.info[_TENANT_CONTEXT_KEY] = validated
    # Begin physically if no connection is held yet: the listener then applies the
    # tenant and marks it. If a transaction was already running (a connection held,
    # after_begin fired before the tenant was known), apply it explicitly. in_transaction()
    # cannot tell these apart: session.add() starts a transaction with no connection.
    sync_session.info.pop(_TENANT_APPLIED_KEY, None)
    await session.connection()
    if not sync_session.info.pop(_TENANT_APPLIED_KEY, False):
        await session.execute(_SET_TENANT_LOCAL, {"tenant_id": validated})


def worker_async_session(*, pin_connection=False):
    """Create a fresh async engine + session for Celery worker tasks.

    Each Celery prefork worker creates its own event loop via asyncio.new_event_loop().
    The module-level engine/session_factory are bound to the main process's loop and
    cannot be reused. This function creates a disposable engine per task invocation.
    Collection may retain a connection across commits to avoid pool checkout/ping
    roundtrips. This does not open an enclosing transaction: each session commit
    remains a real commit and SET LOCAL still ends at its transaction boundary.
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
        try:
            if pin_connection:
                async with _engine.connect() as connection:
                    async with factory(bind=connection) as session:
                        yield session
            else:
                async with factory() as session:
                    yield session
        finally:
            await _engine.dispose()

    return _session()
