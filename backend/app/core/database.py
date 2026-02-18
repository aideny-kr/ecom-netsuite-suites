import ssl
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings


def _is_supabase(url: str) -> bool:
    """Check if the database URL points to Supabase."""
    return "supabase.com" in url or "supabase.co" in url


def _build_connect_args(url: str) -> dict:
    """Build connection args with SSL for Supabase, plain for local."""
    if _is_supabase(url):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        return {"ssl": ssl_ctx}
    return {}


# Prefer direct connection (bypasses PgBouncer) when available
_db_url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
_is_remote = _is_supabase(_db_url)
_pool_size = 5 if _is_remote else 20
_max_overflow = 5 if _is_remote else 10

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
    """Set RLS tenant context for the current database session."""
    await session.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_id}'"))
