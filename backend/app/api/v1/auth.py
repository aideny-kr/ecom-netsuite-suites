import uuid
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.rate_limit import check_login_rate_limit
from app.core.security import decode_token
from app.core.token_denylist import revoke_token
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    SwitchTenantRequest,
    TenantSummary,
    UserProfile,
)
from app.services import audit_service, auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    """Set the refresh token as an HttpOnly cookie (F3)."""
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/api/v1/auth",
        max_age=7 * 24 * 60 * 60,  # 7 days
    )


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    request: RegisterRequest,
    raw_request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
):
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
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant slug or email already exists",
        )

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

    # F3: Set refresh token as HttpOnly cookie, strip from body
    _set_refresh_cookie(response, tokens["refresh_token"])
    return AuthResponse(access_token=tokens["access_token"], token_type=tokens["token_type"])


@router.post("/login", response_model=AuthResponse)
async def login(
    request: LoginRequest,
    raw_request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    # F2: Rate-limit login attempts by IP
    client_ip = raw_request.client.host if raw_request.client else "unknown"
    if not check_login_rate_limit(client_ip):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts")

    try:
        user, tokens = await auth_service.authenticate(db, request.email, request.password)
    except ValueError as e:
        # F8: Audit failed login attempts (use zero UUID as sentinel for unknown tenant)
        await audit_service.log_event(
            db=db,
            tenant_id=uuid.UUID(int=0),
            category="auth",
            action="user.login_failed",
            status="denied",
            payload={"email": request.email, "ip": client_ip},
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="auth",
        action="user.login",
        actor_id=user.id,
    )
    await db.commit()

    # F3: Set refresh token as HttpOnly cookie, strip from body
    _set_refresh_cookie(response, tokens["refresh_token"])
    return AuthResponse(access_token=tokens["access_token"], token_type=tokens["token_type"])


@router.post("/refresh", response_model=AuthResponse)
async def refresh(
    raw_request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    refresh_token: Annotated[str | None, Cookie()] = None,
    body: RefreshRequest | None = None,
):
    # F3: Read refresh token from cookie first, fall back to body for backward compat
    token = refresh_token
    if not token and body:
        token = body.refresh_token
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing refresh token")

    try:
        tokens = await auth_service.refresh_access_token(db, token)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))

    # Set new refresh cookie
    _set_refresh_cookie(response, tokens["refresh_token"])
    return AuthResponse(access_token=tokens["access_token"], token_type=tokens["token_type"])


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    user: Annotated[User, Depends(get_current_user)],
    raw_request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: LogoutRequest | None = None,
):
    """F5: Logout — revoke current access token (and optionally refresh token)."""
    # Revoke the access token
    credentials = raw_request.headers.get("authorization", "").replace("Bearer ", "")
    payload = decode_token(credentials)
    if payload and payload.get("jti"):
        revoke_token(payload["jti"], payload.get("exp", 0))

    # Optionally revoke a refresh token JTI
    if body and body.refresh_token_jti:
        revoke_token(body.refresh_token_jti, 0)

    # Clear refresh cookie
    response.delete_cookie(key="refresh_token", path="/api/v1/auth")

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="auth",
        action="user.logout",
        actor_id=user.id,
    )
    await db.commit()


@router.get("/me", response_model=UserProfile)
async def me(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from app.models.tenant import TenantConfig

    result = await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))
    tenant = result.scalar_one_or_none()

    config_result = await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == user.tenant_id))
    config = config_result.scalar_one_or_none()
    onboarding_completed_at = (
        config.onboarding_completed_at.isoformat() if config and config.onboarding_completed_at else None
    )

    return UserProfile(
        id=str(user.id),
        tenant_id=str(user.tenant_id),
        tenant_name=tenant.name if tenant else "",
        email=user.email,
        full_name=user.full_name,
        actor_type=user.actor_type,
        roles=[ur.role.name for ur in user.user_roles],
        onboarding_completed_at=onboarding_completed_at,
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
        .where(User.email == user.email, User.is_active.is_(True))
    )
    rows = result.all()
    return [TenantSummary(id=str(t.id), name=t.name, slug=t.slug, plan=t.plan) for _, t in rows]


@router.post("/switch-tenant", response_model=AuthResponse)
async def switch_tenant(
    request: SwitchTenantRequest,
    user: Annotated[User, Depends(get_current_user)],
    raw_request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Switch to a different tenant. User must have an account in that tenant with the same email."""
    try:
        target_user, tokens = await auth_service.switch_tenant(db, user.email, request.tenant_id)
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

    # F3: Set refresh token as HttpOnly cookie
    _set_refresh_cookie(response, tokens["refresh_token"])
    return AuthResponse(access_token=tokens["access_token"], token_type=tokens["token_type"])
