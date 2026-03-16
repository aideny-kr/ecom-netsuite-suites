"""Invite service — create, accept, revoke team member invitations."""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, create_refresh_token, hash_password
from app.models.invite import Invite
from app.models.user import Role, User, UserRole
from app.services import entitlement_service
from app.services.email_service import send_invite_email

logger = structlog.get_logger()

VALID_INVITE_ROLES = {"admin", "finance", "ops"}
ROLE_DISPLAY_NAMES = {
    "admin": "Admin",
    "finance": "Finance",
    "ops": "Operations",
    "readonly": "Read Only",
}
INVITE_EXPIRY_DAYS = 7


async def create_invite(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    email: str,
    role_name: str,
    invited_by: uuid.UUID,
    inviter_name: str,
    tenant_brand_name: str,
) -> Invite:
    """Create and send a team invitation."""
    email = email.strip().lower()

    if role_name not in VALID_INVITE_ROLES:
        raise ValueError(f"Invalid role: {role_name}. Must be one of {VALID_INVITE_ROLES}")

    # Check if email is already an active user in this tenant
    existing_user = await db.execute(
        select(User).where(
            User.tenant_id == tenant_id,
            User.email == email,
            User.is_active.is_(True),
        )
    )
    if existing_user.scalar_one_or_none():
        raise ValueError(f"User with email {email} already exists in this tenant")

    # Check for existing pending invite
    existing_invite = await db.execute(
        select(Invite).where(
            Invite.tenant_id == tenant_id,
            Invite.email == email,
            Invite.status == "pending",
        )
    )
    if existing_invite.scalar_one_or_none():
        raise ValueError(f"A pending invitation already exists for {email}")

    # Check seat limit: active users + pending invites vs max_users
    active_users_result = await db.execute(
        select(func.count(User.id)).where(
            User.tenant_id == tenant_id,
            User.is_active.is_(True),
        )
    )
    active_users = active_users_result.scalar() or 0

    pending_invites_result = await db.execute(
        select(func.count(Invite.id)).where(
            Invite.tenant_id == tenant_id,
            Invite.status == "pending",
        )
    )
    pending_invites = pending_invites_result.scalar() or 0

    limits = await entitlement_service.get_plan_limits(db, tenant_id)
    max_users = limits.get("max_users", 20)
    if max_users != -1 and (active_users + pending_invites) >= max_users:
        raise ValueError(
            f"Seat limit reached ({active_users} active + {pending_invites} pending "
            f"= {active_users + pending_invites}/{max_users}). Upgrade your plan for more seats."
        )

    token = secrets.token_urlsafe(32)
    invite = Invite(
        tenant_id=tenant_id,
        email=email,
        role_name=role_name,
        invited_by=invited_by,
        token=token,
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(days=INVITE_EXPIRY_DAYS),
    )
    db.add(invite)

    role_display = ROLE_DISPLAY_NAMES.get(role_name, role_name)
    await send_invite_email(
        to_email=email,
        inviter_name=inviter_name,
        tenant_brand_name=tenant_brand_name,
        role_display_name=role_display,
        token=token,
    )

    logger.info(
        "invite.created",
        tenant_id=str(tenant_id),
        email=email,
        role=role_name,
        invited_by=str(invited_by),
    )
    return invite


async def accept_invite(
    db: AsyncSession,
    token: str,
    full_name: str,
    password: str | None = None,
    google_id_token: str | None = None,
) -> tuple[User, dict]:
    """Accept an invitation and create a user account."""
    result = await db.execute(select(Invite).where(Invite.token == token))
    invite = result.scalar_one_or_none()

    if not invite:
        raise ValueError("Invalid invitation token")

    if invite.status == "accepted":
        raise ValueError("This invitation has already been accepted")

    if invite.status == "revoked":
        raise ValueError("This invitation has been revoked")

    if invite.status == "pending" and invite.expires_at < datetime.now(timezone.utc):
        invite.status = "expired"
        raise ValueError("This invitation has expired")

    if not password and not google_id_token:
        raise ValueError("Must provide either a password or Google ID token to accept invitation")

    # Determine auth method
    auth_provider = "email"
    google_sub = None
    if google_id_token:
        from app.services.google_auth_service import verify_google_token
        import asyncio
        google_info = await verify_google_token(google_id_token)
        if google_info["email"].lower() != invite.email.lower():
            raise ValueError("Google account email does not match the invitation email.")
        auth_provider = "google"
        google_sub = google_info["sub"]
        if not full_name or full_name.strip() == "":
            full_name = google_info.get("name", invite.email)

    # Create user
    hashed = hash_password(password) if password else hash_password(secrets.token_urlsafe(32))
    user = User(
        tenant_id=invite.tenant_id,
        email=invite.email,
        hashed_password=hashed,
        full_name=full_name,
        auth_provider=auth_provider,
        google_sub=google_sub,
        is_active=True,
    )
    db.add(user)
    await db.flush()  # Get user.id

    # Assign role
    role_result = await db.execute(select(Role).where(Role.name == invite.role_name))
    role = role_result.scalar_one_or_none()
    if role:
        user_role = UserRole(
            tenant_id=invite.tenant_id,
            user_id=user.id,
            role_id=role.id,
        )
        db.add(user_role)

    # Mark invite as accepted
    invite.status = "accepted"
    invite.accepted_at = datetime.now(timezone.utc)

    # Generate tokens
    token_data = {
        "sub": str(user.id),
        "tenant_id": str(user.tenant_id),
    }
    tokens = {
        "access_token": create_access_token(token_data),
        "refresh_token": create_refresh_token(token_data),
        "token_type": "bearer",
    }

    logger.info(
        "invite.accepted",
        tenant_id=str(invite.tenant_id),
        email=invite.email,
        user_id=str(user.id),
    )
    return user, tokens


async def revoke_invite(
    db: AsyncSession,
    invite_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """Revoke a pending invitation."""
    result = await db.execute(
        select(Invite).where(
            Invite.id == invite_id,
            Invite.tenant_id == tenant_id,
        )
    )
    invite = result.scalar_one_or_none()

    if not invite:
        raise ValueError("Invitation not found")

    if invite.status != "pending":
        raise ValueError("Only pending invitations can be revoked")

    invite.status = "revoked"
    logger.info(
        "invite.revoked",
        tenant_id=str(tenant_id),
        invite_id=str(invite_id),
        email=invite.email,
    )


async def list_invites(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> list[Invite]:
    """Return pending and accepted invites for a tenant."""
    result = await db.execute(
        select(Invite)
        .where(
            Invite.tenant_id == tenant_id,
            Invite.status.in_(["pending", "accepted"]),
        )
        .order_by(Invite.created_at.desc())
    )
    return list(result.scalars().all())


async def get_invite_by_token(
    db: AsyncSession,
    token: str,
) -> Invite | None:
    """Look up an invite by its token."""
    result = await db.execute(select(Invite).where(Invite.token == token))
    return result.scalar_one_or_none()
