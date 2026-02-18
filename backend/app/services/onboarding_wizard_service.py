import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.onboarding_checklist import STEP_KEYS, OnboardingChecklistItem
from app.models.tenant import TenantConfig
from app.services.audit_service import log_event

logger = structlog.get_logger()


async def get_checklist(db: AsyncSession, tenant_id: uuid.UUID) -> list[OnboardingChecklistItem]:
    """Return all 5 checklist items, creating them lazily if missing."""
    result = await db.execute(select(OnboardingChecklistItem).where(OnboardingChecklistItem.tenant_id == tenant_id))
    items = {item.step_key: item for item in result.scalars().all()}

    created_any = False
    for key in STEP_KEYS:
        if key not in items:
            item = OnboardingChecklistItem(tenant_id=tenant_id, step_key=key, status="pending")
            db.add(item)
            items[key] = item
            created_any = True

    if created_any:
        await db.flush()

    return [items[k] for k in STEP_KEYS]


async def complete_step(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    step_key: str,
    user_id: uuid.UUID,
    metadata: dict | None = None,
) -> OnboardingChecklistItem:
    items = await get_checklist(db, tenant_id)
    item = next((i for i in items if i.step_key == step_key), None)
    if not item:
        raise ValueError(f"Invalid step_key: {step_key}")

    item.status = "completed"
    item.completed_at = datetime.now(timezone.utc)
    item.completed_by = user_id
    if metadata:
        item.metadata_ = metadata
    await db.flush()

    correlation_id = str(uuid.uuid4())
    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="onboarding",
        action="onboarding.step_completed",
        actor_id=user_id,
        resource_type="onboarding_checklist",
        resource_id=step_key,
        correlation_id=correlation_id,
        payload={"step_key": step_key, "metadata": metadata},
    )
    return item


async def skip_step(
    db: AsyncSession, tenant_id: uuid.UUID, step_key: str, user_id: uuid.UUID
) -> OnboardingChecklistItem:
    items = await get_checklist(db, tenant_id)
    item = next((i for i in items if i.step_key == step_key), None)
    if not item:
        raise ValueError(f"Invalid step_key: {step_key}")

    item.status = "skipped"
    item.completed_at = datetime.now(timezone.utc)
    item.completed_by = user_id
    await db.flush()

    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="onboarding",
        action="onboarding.step_skipped",
        actor_id=user_id,
        resource_type="onboarding_checklist",
        resource_id=step_key,
        payload={"step_key": step_key},
    )
    return item


async def validate_step(db: AsyncSession, tenant_id: uuid.UUID, step_key: str) -> dict:
    """Validate that a step's requirements are met by checking backend data."""
    if step_key == "profile":
        from app.services.onboarding_service import get_active_profile

        profile = await get_active_profile(db, tenant_id)
        if profile:
            return {"step_key": step_key, "valid": True}
        return {"step_key": step_key, "valid": False, "reason": "No confirmed tenant profile found"}

    elif step_key == "connection":
        from app.models.connection import Connection

        result = await db.execute(
            select(Connection).where(Connection.tenant_id == tenant_id, Connection.status == "active")
        )
        conn = result.scalars().first()
        if conn:
            return {"step_key": step_key, "valid": True}
        return {"step_key": step_key, "valid": False, "reason": "No active NetSuite connection found"}

    elif step_key == "policy":
        from app.services.policy_service import get_active_policy

        policy = await get_active_policy(db, tenant_id)
        if policy:
            return {"step_key": step_key, "valid": True}
        return {"step_key": step_key, "valid": False, "reason": "No active policy profile found"}

    elif step_key == "workspace":
        from app.models.workspace import Workspace

        result = await db.execute(select(Workspace).where(Workspace.tenant_id == tenant_id).limit(1))
        ws = result.scalars().first()
        if ws:
            return {"step_key": step_key, "valid": True}
        return {"step_key": step_key, "valid": False, "reason": "No workspace found"}

    elif step_key == "first_success":
        from app.models.workspace import Workspace, WorkspaceRun

        result = await db.execute(
            select(WorkspaceRun.run_type).where(
                WorkspaceRun.workspace_id.in_(select(Workspace.id).where(Workspace.tenant_id == tenant_id)),
                WorkspaceRun.status == "passed",
                WorkspaceRun.run_type.in_(("sdf_validate", "jest_unit_test")),
            )
        )
        passed_types = set(result.scalars().all())
        required = {"sdf_validate", "jest_unit_test"}
        missing = sorted(required - passed_types)
        if not missing:
            return {"step_key": step_key, "valid": True}
        return {
            "step_key": step_key,
            "valid": False,
            "reason": f"Missing passing runs for: {', '.join(missing)}",
        }

    return {"step_key": step_key, "valid": False, "reason": f"Unknown step: {step_key}"}


async def finalize_onboarding(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> datetime:
    """Finalize onboarding: validate required steps, set onboarding_completed_at."""
    items = await get_checklist(db, tenant_id)
    # "profile" is required, others can be skipped
    required_steps = {"profile"}
    for item in items:
        if item.step_key in required_steps and item.status == "pending":
            raise ValueError(f"Required step '{item.step_key}' is not completed")

    now = datetime.now(timezone.utc)
    result = await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_id))
    config = result.scalar_one_or_none()
    if config:
        config.onboarding_completed_at = now
    await db.flush()

    from app.services.policy_service import lock_active_policy

    await lock_active_policy(db, tenant_id, user_id)

    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="onboarding",
        action="onboarding.finalized",
        actor_id=user_id,
        resource_type="tenant_config",
        resource_id=str(tenant_id),
        payload={"steps": {i.step_key: i.status for i in items}},
    )
    return now


async def get_audit_trail(db: AsyncSession, tenant_id: uuid.UUID) -> list:
    from app.models.audit import AuditEvent

    result = await db.execute(
        select(AuditEvent)
        .where(AuditEvent.tenant_id == tenant_id, AuditEvent.category == "onboarding")
        .order_by(AuditEvent.timestamp.desc())
    )
    return list(result.scalars().all())
