"""Operator-only bootstrap for a dedicated, initially empty database.

No HTTP route calls this service. The database advisory lock serializes concurrent
installers; public registration is disabled in SINGLE_COMPANY mode. Nothing here
converts existing tenants, rotates passwords, seeds soul files, or enables autonomy.
"""

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import set_tenant_context
from app.core.security import hash_password
from app.models.feature_flag import TenantFeatureFlag
from app.models.tenant import Tenant, TenantConfig
from app.models.user import Permission, Role, RolePermission, User, UserRole
from app.schemas.auth import RegisterRequest
from app.services.audit_service import log_event
from app.services.feature_flag_service import DEFAULT_FLAGS

# A stable, database-local transaction lock; not Python's randomized hash().
_BOOTSTRAP_LOCK = 731920260913
_SYSTEM_TENANT = uuid.UUID(int=0)  # Migration 080 seeds the shared metric catalog owner.
_COMPANY_FLAGS = {"mcp_tools", "byok_ai", "custom_branding", "reconciliation", "celigo", "recon_resolution_ui"}


async def adopt_existing_company(
    db: AsyncSession,
    *,
    expected_tenant_id: uuid.UUID,
    expected_slug: str,
    expected_database: str,
    apply: bool = False,
) -> tuple[Tenant, bool]:
    """Convert an already-isolated company; preview by default, never extract/delete.

    The operator supplies the exact destination database, company UUID and slug.
    Only the commercial plan/expiry change. Company configuration, permissions,
    feature flags, credentials, schedules, approvals and workspace files survive.
    This service owns its transaction and must use an otherwise clean session.
    """
    if not settings.SINGLE_COMPANY:
        raise ValueError("Existing-company adoption requires SINGLE_COMPANY=true")
    if db.new or db.dirty or db.deleted:
        raise ValueError("Adoption requires a clean operator session")
    if not expected_database or not expected_slug or expected_tenant_id == _SYSTEM_TENANT:
        raise ValueError("An explicit destination database and company identity are required")
    try:
        database = (await db.execute(text("SELECT current_database()"))).scalar_one()
        if database != expected_database:
            raise ValueError("Destination database does not match the expected database")
        # Serialize with bootstrap; block concurrent tenant creation until commit.
        # row_security=off raises if RLS would hide a second company from this
        # inventory. This command needs operator authority, never runtime bypass.
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BOOTSTRAP_LOCK})
        await db.execute(text("SET LOCAL row_security = off"))
        await db.execute(text("LOCK TABLE tenants IN SHARE ROW EXCLUSIVE MODE"))
        tenants = (await db.execute(select(Tenant).where(Tenant.id != _SYSTEM_TENANT).limit(2))).scalars().all()
        if len(tenants) != 1:
            raise ValueError("Adoption requires exactly one company in an isolated database; export/import first")
        tenant = tenants[0]
        if tenant.id != expected_tenant_id or tenant.slug != expected_slug:
            raise ValueError("Company identity does not match the expected UUID and slug")
        if not tenant.is_active:
            raise ValueError("The company is deactivated; adoption will not reactivate it")
        await set_tenant_context(db, str(tenant.id))
        admin = (
            await db.execute(
                select(User.id)
                .join(UserRole, UserRole.user_id == User.id)
                .join(Role, Role.id == UserRole.role_id)
                .where(
                    User.tenant_id == tenant.id,
                    UserRole.tenant_id == tenant.id,
                    User.is_active.is_(True),
                    Role.name == "admin",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if admin is None:
            raise ValueError("An active company administrator is required; adoption will not grant roles")
        changed = tenant.plan != "self_hosted" or tenant.plan_expires_at is not None
        if not apply or not changed:
            # Release locks even for previews and idempotent reruns. No DML.
            await db.commit()
            return tenant, False
        previous = {
            "plan": tenant.plan,
            "plan_expires_at": (tenant.plan_expires_at.isoformat() if tenant.plan_expires_at else None),
        }
        tenant.plan = "self_hosted"
        tenant.plan_expires_at = None
        await log_event(
            db,
            tenant.id,
            category="auth",
            action="company.adopt",
            actor_type="system",
            resource_type="tenant",
            resource_id=str(tenant.id),
            payload={"before": previous, "after": {"plan": "self_hosted", "plan_expires_at": None}},
        )
        await db.commit()
        return tenant, True
    except Exception:
        await db.rollback()
        raise


async def validate_company_database(db: AsyncSession) -> Tenant:
    """Fail closed if dedicated mode points at an empty or shared database."""
    tenants = (await db.execute(select(Tenant).where(Tenant.id != _SYSTEM_TENANT).limit(2))).scalars().all()
    if len(tenants) != 1:
        raise ValueError("Single-company mode requires exactly one company; run bootstrap on a dedicated database")
    tenant = tenants[0]
    if tenant.plan != "self_hosted" or tenant.plan_expires_at is not None:
        raise ValueError("This database has not been bootstrapped for single-company mode")
    if not tenant.is_active:
        raise ValueError("The company is deactivated")
    return tenant


async def bootstrap_company(db: AsyncSession, request: RegisterRequest) -> tuple[Tenant, bool]:
    """Return (company, created). Commit company, admin, flags and audit atomically."""
    if not settings.SINGLE_COMPANY:
        raise ValueError("Set SINGLE_COMPANY=true before bootstrapping a dedicated database")

    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BOOTSTRAP_LOCK})
    existing = (await db.execute(select(Tenant).where(Tenant.id != _SYSTEM_TENANT).limit(2))).scalars().all()
    if existing:
        tenant = await validate_company_database(db)
        await set_tenant_context(db, str(tenant.id))
        admin = (
            await db.execute(
                select(User.id)
                .join(UserRole, UserRole.user_id == User.id)
                .join(Role, Role.id == UserRole.role_id)
                .where(
                    User.tenant_id == tenant.id,
                    UserRole.tenant_id == tenant.id,
                    User.email == str(request.email).lower(),
                    User.is_active.is_(True),
                    Role.name == "admin",
                )
            )
        ).scalar_one_or_none()
        if tenant.slug != request.tenant_slug or admin is None:
            raise ValueError("A different company or administrator already exists; bootstrap will not change it")
        return tenant, False

    role = (await db.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        raise ValueError("Admin role is missing; run migrations before bootstrap")
    permissions = set(
        (
            await db.execute(
                select(Permission.codename)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == role.id)
            )
        ).scalars()
    )
    if not {"tenant.manage", "connections.manage", "schedules.manage"} <= permissions:
        raise ValueError("Admin permissions are incomplete; run all migrations before bootstrap")

    tenant = Tenant(name=request.tenant_name, slug=request.tenant_slug, plan="self_hosted", is_active=True)
    db.add(tenant)
    await db.flush()
    await set_tenant_context(db, str(tenant.id))
    db.add(TenantConfig(tenant_id=tenant.id, posting_mode="lumpsum", posting_batch_size=100))
    user = User(
        tenant_id=tenant.id,
        email=str(request.email).lower(),
        hashed_password=hash_password(request.password),
        full_name=request.full_name,
        actor_type="user",
    )
    db.add(user)
    await db.flush()
    db.add(UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id))
    for key, enabled in DEFAULT_FLAGS.items():
        db.add(TenantFeatureFlag(tenant_id=tenant.id, flag_key=key, enabled=enabled or key in _COMPANY_FLAGS))
    await log_event(
        db,
        tenant.id,
        category="auth",
        action="company.bootstrap",
        actor_id=user.id,
        resource_type="tenant",
        resource_id=str(tenant.id),
    )
    await db.commit()
    return tenant, True
