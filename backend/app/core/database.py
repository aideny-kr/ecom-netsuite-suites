import ssl
import uuid
from collections.abc import AsyncGenerator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

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


_sync_session_factory: sessionmaker | None = None


def _init_sync_sessionmaker() -> None:
    """Lazy-initialize the sync session factory, reusing base_task's engine.

    base_task.py already creates ``sync_engine = create_engine(DATABASE_URL_SYNC)``
    at module level. Reusing it avoids a second connection pool per worker
    process. base_task is always imported before this factory is used because
    Celery initialises task modules (which import base_task) during worker
    startup.
    """
    global _sync_session_factory
    if _sync_session_factory is None:
        try:
            # Reuse the engine already created in base_task to avoid double
            # pool allocation in Celery worker processes.
            from app.workers.base_task import sync_engine as _base_sync_engine
            _sync_session_factory = sessionmaker(bind=_base_sync_engine)
        except ImportError:
            # Fallback for contexts where base_task is not available
            # (e.g., alembic, standalone scripts).
            _fallback_engine = create_engine(settings.DATABASE_URL_SYNC, pool_pre_ping=True)
            _sync_session_factory = sessionmaker(bind=_fallback_engine)


@contextmanager
def get_sync_session():
    """Sync SQLAlchemy session for Celery workers.

    Uses DATABASE_URL_SYNC (psycopg2 driver). Reuses the sync engine from
    base_task.py so there is only one connection pool per worker process.

    Suitable for ProgressEmitter and finalize_run_sync in the Celery worker.

    Pool-size note: sessions can be held for 30+ minutes (benchmark runs), so
    concurrent agent-lab runs consume connections from SQLAlchemy's default pool
    (size 5 from base_task's create_engine defaults). Acceptable for v1
    single-super-admin usage; v1.1 may need pool_size tuning if concurrent runs
    are added.
    """
    _init_sync_sessionmaker()
    session: Session = _sync_session_factory()  # type: ignore[misc]
    try:
        yield session
    finally:
        session.close()


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
