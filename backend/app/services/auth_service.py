import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, create_refresh_token, decode_token, hash_password, verify_password
from app.models.tenant import Tenant, TenantConfig
from app.models.user import Role, User, UserRole


async def register_tenant(
    db: AsyncSession,
    tenant_name: str,
    tenant_slug: str,
    email: str,
    password: str,
    full_name: str,
) -> tuple[Tenant, User, dict]:
    """Register a new tenant with admin user. Returns (tenant, user, tokens)."""
    # Check slug uniqueness
    existing = await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
    if existing.scalar_one_or_none():
        raise ValueError("Tenant slug already exists")

    # Create tenant
    tenant = Tenant(
        name=tenant_name,
        slug=tenant_slug,
        plan="trial",
        plan_expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        is_active=True,
    )
    db.add(tenant)
    await db.flush()

    # Create tenant config
    config = TenantConfig(
        tenant_id=tenant.id,
        posting_mode="lumpsum",
        posting_batch_size=100,
        posting_attach_evidence=False,
    )
    db.add(config)

    # Create admin user
    user = User(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password(password),
        full_name=full_name,
        actor_type="user",
    )
    db.add(user)
    await db.flush()

    # Assign admin role
    admin_role = await db.execute(select(Role).where(Role.name == "admin"))
    role = admin_role.scalar_one_or_none()
    if role:
        user_role = UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id)
        db.add(user_role)

    await db.commit()
    await db.refresh(tenant)
    await db.refresh(user)

    tokens = _create_tokens(user)
    return tenant, user, tokens


async def authenticate(db: AsyncSession, email: str, password: str) -> tuple[User, dict]:
    """Authenticate user and return tokens."""
    result = await db.execute(select(User).where(User.email == email, User.is_active.is_(True)))
    users = result.scalars().all()

    user = None
    for candidate in users:
        if verify_password(password, candidate.hashed_password):
            user = candidate
            break

    if not user:
        raise ValueError("Invalid email or password")

    tokens = _create_tokens(user)
    return user, tokens


async def refresh_access_token(db: AsyncSession, refresh_token: str) -> dict:
    """Refresh access token using refresh token."""
    payload = decode_token(refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise ValueError("Invalid refresh token")

    user_id = payload.get("sub")
    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id), User.is_active.is_(True)))
    user = result.scalar_one_or_none()
    if not user:
        raise ValueError("User not found")

    return _create_tokens(user)


async def switch_tenant(db: AsyncSession, email: str, tenant_id: str) -> tuple[User, dict]:
    """Switch to a different tenant by finding the user's account in that tenant."""
    result = await db.execute(
        select(User).where(
            User.email == email,
            User.tenant_id == uuid.UUID(tenant_id),
            User.is_active.is_(True),
        )
    )
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise ValueError("You do not have an account in that tenant")

    tokens = _create_tokens(target_user)
    return target_user, tokens


def _create_tokens(user: User) -> dict:
    token_data = {"sub": str(user.id), "tenant_id": str(user.tenant_id)}
    return {
        "access_token": create_access_token(token_data),
        "refresh_token": create_refresh_token(token_data),
        "token_type": "bearer",
    }
