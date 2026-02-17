import uuid
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.security import decode_token
from app.models.user import Permission, RolePermission, User, UserRole

logger = structlog.get_logger()
security = HTTPBearer()


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
) -> User:
    token = credentials.credentials
    payload = decode_token(token)
    if payload is None or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    result = await db.execute(
        select(User)
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
        .where(User.id == uuid.UUID(user_id), User.is_active.is_(True))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    # Set RLS context
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

    # Bind structured logging context
    structlog.contextvars.bind_contextvars(
        tenant_id=str(user.tenant_id),
        user_id=str(user.id),
    )

    request.state.user = user
    request.state.tenant_id = user.tenant_id
    return user


def require_permission(codename: str):
    async def permission_checker(
        user: Annotated[User, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        role_ids = [ur.role_id for ur in user.user_roles]
        if not role_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No roles assigned")

        result = await db.execute(
            select(Permission.codename)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id.in_(role_ids))
        )
        user_permissions = {row[0] for row in result.all()}

        if codename not in user_permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {codename}",
            )
        return user

    return permission_checker


def require_entitlement(feature: str):
    async def entitlement_checker(
        user: Annotated[User, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        from app.services.entitlement_service import check_entitlement

        allowed = await check_entitlement(db, user.tenant_id, feature)
        if not allowed:
            logger.warning("entitlement_denied", feature=feature, tenant_id=str(user.tenant_id))
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Feature '{feature}' not available on your current plan",
            )
        return user

    return entitlement_checker
