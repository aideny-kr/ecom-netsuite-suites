import uuid
from functools import wraps
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core.security import decode_token
from app.models.user import User, UserRole, Role, RolePermission, Permission
from app.models.tenant import Tenant

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
        .where(User.id == uuid.UUID(user_id), User.is_active == True)
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
        result = await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))
        tenant = result.scalar_one_or_none()
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant not found")

        # Trial plan limits
        plan_features = {
            "trial": {"connections": 2, "tables": True, "exports": True, "mcp_tools": False},
            "pro": {"connections": 50, "tables": True, "exports": True, "mcp_tools": True},
            "enterprise": {"connections": 500, "tables": True, "exports": True, "mcp_tools": True},
        }
        features = plan_features.get(tenant.plan, plan_features["trial"])

        if feature in features:
            allowed = features[feature]
            if isinstance(allowed, bool) and not allowed:
                logger.warning("entitlement_denied", feature=feature, plan=tenant.plan)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Feature '{feature}' not available on {tenant.plan} plan",
                )
        return user

    return entitlement_checker
