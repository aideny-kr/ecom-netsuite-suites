from fastapi import APIRouter
from sqlalchemy import text

from app.core.database import engine
from app.schemas.common import HealthResponse

router = APIRouter(tags=["health"])

# Active SSE connection counter (per-worker — each gunicorn worker has its own process)
_active_sse_count: int = 0


def increment_sse() -> None:
    global _active_sse_count
    _active_sse_count += 1


def decrement_sse() -> None:
    global _active_sse_count
    _active_sse_count = max(0, _active_sse_count - 1)


@router.get("/health", response_model=HealthResponse)
async def health_check():
    db_status = "ok"
    redis_status = "ok"

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        db_status = "error"

    try:
        import redis.asyncio as aioredis

        from app.core.config import settings

        r = aioredis.from_url(settings.REDIS_URL)
        await r.ping()
        await r.aclose()
    except Exception:
        redis_status = "error"

    overall = "ok" if db_status == "ok" and redis_status == "ok" else "degraded"
    return HealthResponse(status=overall, database=db_status, redis=redis_status)


@router.get("/health/detailed")
async def health_detailed():
    """Detailed health check with pool utilization, active SSE count, and Redis status."""
    pool = engine.pool

    # DB pool stats
    db_pool = {
        "pool_size": pool.size(),
        "checked_out": pool.checkedout(),
        "overflow": pool.overflow(),
        "checked_in": pool.checkedin(),
        "max_overflow": pool._max_overflow,
    }

    # Redis status
    redis_ok = False
    try:
        import redis.asyncio as aioredis

        from app.core.config import settings

        r = aioredis.from_url(settings.REDIS_URL)
        await r.ping()
        await r.aclose()
        redis_ok = True
    except Exception:
        pass

    return {
        "status": "ok" if redis_ok else "degraded",
        "db_pool": db_pool,
        "active_sse_connections": _active_sse_count,
        "redis_connected": redis_ok,
    }
