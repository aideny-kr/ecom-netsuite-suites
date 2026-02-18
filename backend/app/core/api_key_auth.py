"""FastAPI dependency for API key authentication (X-API-Key header)."""

import uuid

import structlog
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.tenant import Tenant
from app.services.chat_api_key_service import authenticate_key

logger = structlog.get_logger()


class ApiKeyContext:
    """Holds the authenticated API key context."""

    def __init__(self, tenant_id: uuid.UUID, scopes: list[str]):
        self.tenant_id = tenant_id
        self.scopes = scopes


async def get_api_key_context(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ApiKeyContext:
    """Extract and validate X-API-Key header, set RLS, return context."""
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )

    try:
        tenant_id, scopes = await authenticate_key(db, api_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        )

    # Check tenant is active
    from sqlalchemy import select

    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant or not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant is deactivated",
        )

    # Set RLS context
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_id}'"))

    # Bind structured logging context
    structlog.contextvars.bind_contextvars(
        tenant_id=str(tenant_id),
        auth_method="api_key",
    )

    request.state.tenant_id = tenant_id
    request.state.api_key_scopes = scopes

    return ApiKeyContext(tenant_id=tenant_id, scopes=scopes)
