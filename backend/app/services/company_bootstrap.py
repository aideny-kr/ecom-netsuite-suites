"""Operator-only fresh bootstrap and guarded adoption of an isolated company.

No HTTP route calls these services. Both serialize with the same advisory lock.
Adoption only removes commercial plan restrictions; neither path rotates existing
credentials, overwrites company instructions or supplies financial approval.
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


def company_entitlement_changes(plan: str) -> dict:
    """Expose the commercial capability change without changing feature flags."""
    from app.services.entitlement_service import PLAN_LIMITS

    before = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    after = PLAN_LIMITS["self_hosted"]
    return {
        key: {"before": before.get(key), "after": value} for key, value in after.items() if before.get(key) != value
    }


async def adopt_existing_company(
    db: AsyncSession,
    *,
    expected_tenant_id: uuid.UUID,
    expected_slug: str,
    expected_database: str,
    expected_system_identifier: str,
    apply: bool = False,
) -> tuple[Tenant, bool]:
    """Convert an already-isolated company; preview by default, never extract/delete.

    The operator supplies the destination cluster/database, company UUID and slug.
    Only the commercial plan/expiry change. Company configuration, permissions,
    feature flags, credentials, schedules, approvals and workspace files survive.
    This service owns its transaction and must use an otherwise clean session.
    """
    if not settings.SINGLE_COMPANY:
        raise ValueError("Existing-company adoption requires SINGLE_COMPANY=true")
    if db.new or db.dirty or db.deleted:
        raise ValueError("Adoption requires a clean operator session")
    if (
        not expected_database
        or not expected_slug
        or expected_tenant_id == _SYSTEM_TENANT
        or not expected_system_identifier.isdecimal()
    ):
        raise ValueError("An explicit destination cluster/database and company identity are required")
    try:
        database = (await db.execute(text("SELECT current_database()"))).scalar_one()
        if database != expected_database:
            raise ValueError("Destination database does not match the expected database")
        cluster = (await db.execute(text("SELECT system_identifier::text FROM pg_control_system()"))).scalar_one()
        if cluster != expected_system_identifier:
            raise ValueError("Destination cluster does not match the expected system identifier")
        # Serialize with bootstrap; block concurrent tenant creation until commit.
        # tenants itself has no RLS. row_security=off makes the subsequent scoped
        # table inventory fail if RLS hides rows from this operator connection.
        await db.execute(text("SET LOCAL lock_timeout = '5s'"))
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
        tables = (
            (
                await db.execute(
                    text(
                        "SELECT c.relname FROM pg_catalog.pg_class c "
                        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                        "JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid "
                        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
                        "AND a.attname = 'tenant_id' AND a.attnum > 0 AND NOT a.attisdropped ORDER BY c.relname"
                    )
                )
            )
            .scalars()
            .all()
        )
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            await db.execute(text(f"LOCK TABLE public.{quoted} IN SHARE MODE"))
            foreign = (
                await db.execute(
                    text(f"SELECT 1 FROM public.{quoted} WHERE tenant_id NOT IN (:company, :system) LIMIT 1"),
                    {"company": tenant.id, "system": _SYSTEM_TENANT},
                )
            ).first()
            if foreign:
                raise ValueError("Destination contains rows belonging to another company; verify the export/import")
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
        entitlements = company_entitlement_changes(tenant.plan)
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
            payload={
                "before": previous,
                "after": {"plan": "self_hosted", "plan_expires_at": None},
                "entitlement_changes": entitlements,
                "destination_system_identifier": cluster,
            },
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
