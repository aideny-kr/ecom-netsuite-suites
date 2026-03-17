"""Invite API endpoints — create, list, revoke, accept team invitations."""

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_permission
from app.models.tenant import Tenant, TenantConfig
from app.models.user import User
from app.schemas.auth import AuthResponse
from app.services import audit_service, invite_service
from app.services.invite_service import ROLE_DISPLAY_NAMES

router = APIRouter(prefix="/invites", tags=["invites"])

# Module-level permission checker so tests can override via dependency_overrides
_users_manage = require_permission("users.manage")


# ---------- Schemas ----------


class InviteCreate(BaseModel):
    email: EmailStr
    role_name: str = Field(default="finance", min_length=1, max_length=50)


class InviteResponse(BaseModel):
    id: str
    email: str
    role_name: str
    role_display_name: str
    status: str
    expires_at: datetime
    created_at: datetime


class InviteAcceptRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=8)
    google_id_token: str | None = None


class InviteAcceptInfo(BaseModel):
    email: str
    role_name: str
    role_display_name: str
    tenant_name: str
    status: str
    expired: bool


# ---------- Helpers ----------


async def _get_tenant_name(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    """Return the brand name or tenant name for display."""
    config_result = await db.execute(
        select(TenantConfig.brand_name).where(TenantConfig.tenant_id == tenant_id)
    )
    brand_name = config_result.scalar_one_or_none()
    if brand_name:
        return brand_name

    tenant_result = await db.execute(select(Tenant.name).where(Tenant.id == tenant_id))
    return tenant_result.scalar_one_or_none() or "Your Organization"


# ---------- Protected endpoints ----------


@router.post("", response_model=InviteResponse, status_code=status.HTTP_201_CREATED)
async def create_invite_endpoint(
    request: InviteCreate,
    user: Annotated[User, Depends(_users_manage)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create and send a team invitation."""
    tenant_name = await _get_tenant_name(db, user.tenant_id)
    try:
        inv = await invite_service.create_invite(
            db=db,
            tenant_id=user.tenant_id,
            email=request.email,
            role_name=request.role_name,
            invited_by=user.id,
            inviter_name=user.full_name,
            tenant_brand_name=tenant_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="invite",
        action="invite.create",
        actor_id=user.id,
        resource_type="invite",
        resource_id=str(inv.id),
    )
    await db.commit()

    return InviteResponse(
        id=str(inv.id),
        email=inv.email,
        role_name=inv.role_name,
        role_display_name=ROLE_DISPLAY_NAMES.get(inv.role_name, inv.role_name),
        status=inv.status,
        expires_at=inv.expires_at,
        created_at=inv.created_at,
    )


@router.get("", response_model=list[InviteResponse])
async def list_invites_endpoint(
    user: Annotated[User, Depends(_users_manage)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List pending and accepted invites for the tenant."""
    invites = await invite_service.list_invites(db=db, tenant_id=user.tenant_id)
    return [
        InviteResponse(
            id=str(inv.id),
            email=inv.email,
            role_name=inv.role_name,
            role_display_name=ROLE_DISPLAY_NAMES.get(inv.role_name, inv.role_name),
            status=inv.status,
            expires_at=inv.expires_at,
            created_at=inv.created_at,
        )
        for inv in invites
    ]


@router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite_endpoint(
    invite_id: uuid.UUID,
    user: Annotated[User, Depends(_users_manage)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Revoke a pending invitation."""
    try:
        await invite_service.revoke_invite(db=db, invite_id=invite_id, tenant_id=user.tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="invite",
        action="invite.revoke",
        actor_id=user.id,
        resource_type="invite",
        resource_id=str(invite_id),
    )
    await db.commit()


# ---------- Public endpoints (no auth) ----------


@router.get("/accept/{token}", response_model=InviteAcceptInfo)
async def get_invite_info(
    token: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get invite details for the accept page (public)."""
    inv = await invite_service.get_invite_by_token(db=db, token=token)
    if not inv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")

    tenant_name = await _get_tenant_name(db, inv.tenant_id)
    expired = inv.status == "pending" and inv.expires_at < datetime.now(timezone.utc)

    return InviteAcceptInfo(
        email=inv.email,
        role_name=inv.role_name,
        role_display_name=ROLE_DISPLAY_NAMES.get(inv.role_name, inv.role_name),
        tenant_name=tenant_name,
        status=inv.status,
        expired=expired,
    )


@router.post("/accept/{token}", response_model=AuthResponse)
async def accept_invite_endpoint(
    token: str,
    request: InviteAcceptRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Accept an invitation and create a user account (public)."""
    try:
        user, tokens = await invite_service.accept_invite(
            db=db,
            token=token,
            full_name=request.full_name,
            password=request.password,
            google_id_token=request.google_id_token,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except NotImplementedError as e:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(e))

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="invite",
        action="invite.accept",
        actor_id=user.id,
        resource_type="user",
        resource_id=str(user.id),
    )
    await db.commit()

    # Set refresh token as HttpOnly cookie (matching auth.py pattern)
    response.set_cookie(
        key="refresh_token",
        value=tokens["refresh_token"],
        httponly=True,
        secure=True,
        samesite="lax",
        path="/api/v1/auth",
        max_age=7 * 24 * 60 * 60,
    )

    return AuthResponse(
        access_token=tokens["access_token"],
        token_type=tokens.get("token_type", "bearer"),
    )
