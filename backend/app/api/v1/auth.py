from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.models.tenant import Tenant
from app.schemas.auth import AuthResponse, LoginRequest, RefreshRequest, RegisterRequest, SwitchTenantRequest, TenantSummary, UserProfile
from app.services import auth_service, audit_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(request: RegisterRequest, db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        tenant, user, tokens = await auth_service.register_tenant(
            db=db,
            tenant_name=request.tenant_name,
            tenant_slug=request.tenant_slug,
            email=request.email,
            password=request.password,
            full_name=request.full_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log_event(
        db=db,
        tenant_id=tenant.id,
        category="auth",
        action="tenant.register",
        actor_id=user.id,
        resource_type="tenant",
        resource_id=str(tenant.id),
    )
    await db.commit()
    return tokens


@router.post("/login", response_model=AuthResponse)
async def login(request: LoginRequest, db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        user, tokens = await auth_service.authenticate(db, request.email, request.password)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="auth",
        action="user.login",
        actor_id=user.id,
    )
    await db.commit()
    return tokens


@router.post("/refresh", response_model=AuthResponse)
async def refresh(request: RefreshRequest, db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        tokens = await auth_service.refresh_access_token(db, request.refresh_token)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    return tokens


@router.get("/me", response_model=UserProfile)
async def me(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))
    tenant = result.scalar_one_or_none()
    return UserProfile(
        id=str(user.id),
        tenant_id=str(user.tenant_id),
        tenant_name=tenant.name if tenant else "",
        email=user.email,
        full_name=user.full_name,
        actor_type=user.actor_type,
        roles=[ur.role.name for ur in user.user_roles],
    )


@router.get("/me/tenants", response_model=list[TenantSummary])
async def list_my_tenants(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List all tenants the current user's email belongs to."""
    result = await db.execute(
        select(User, Tenant)
        .join(Tenant, User.tenant_id == Tenant.id)
        .where(User.email == user.email, User.is_active == True)
    )
    rows = result.all()
    return [
        TenantSummary(id=str(t.id), name=t.name, slug=t.slug, plan=t.plan)
        for _, t in rows
    ]


@router.post("/switch-tenant", response_model=AuthResponse)
async def switch_tenant(
    request: SwitchTenantRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Switch to a different tenant. User must have an account in that tenant with the same email."""
    try:
        target_user, tokens = await auth_service.switch_tenant(
            db, user.email, request.tenant_id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    await audit_service.log_event(
        db=db,
        tenant_id=target_user.tenant_id,
        category="auth",
        action="user.switch_tenant",
        actor_id=target_user.id,
        resource_type="tenant",
        resource_id=request.tenant_id,
    )
    await db.commit()
    return tokens
