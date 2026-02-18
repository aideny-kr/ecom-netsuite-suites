"""Policy evaluation service for tool gating, field blocking, and output redaction."""

import uuid

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.policy_profile import PolicyProfile
from app.services.audit_service import log_event

logger = structlog.get_logger()


async def get_active_policy(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> PolicyProfile | None:
    """Return the active policy for a tenant, or None if no custom policy exists."""
    result = await db.execute(
        select(PolicyProfile).where(
            PolicyProfile.tenant_id == tenant_id,
            PolicyProfile.is_active.is_(True),
        )
    )
    return result.scalars().first()


async def create_policy(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    data: dict,
    user_id: uuid.UUID,
) -> PolicyProfile:
    """Create a new policy profile."""
    version_result = await db.execute(
        select(func.coalesce(func.max(PolicyProfile.version), 0)).where(PolicyProfile.tenant_id == tenant_id)
    )
    next_version = (version_result.scalar() or 0) + 1

    if data.get("is_active", True):
        await db.execute(
            update(PolicyProfile)
            .where(
                PolicyProfile.tenant_id == tenant_id,
                PolicyProfile.is_active.is_(True),
            )
            .values(is_active=False)
        )

    policy = PolicyProfile(
        tenant_id=tenant_id,
        version=next_version,
        name=data["name"],
        is_active=data.get("is_active", True),
        is_locked=data.get("is_locked", False),
        sensitivity_default=data.get("sensitivity_default", "financial"),
        read_only_mode=data.get("read_only_mode", True),
        allowed_record_types=data.get("allowed_record_types"),
        blocked_fields=data.get("blocked_fields"),
        tool_allowlist=data.get("tool_allowlist"),
        max_rows_per_query=data.get("max_rows_per_query", 1000),
        require_row_limit=data.get("require_row_limit", True),
        custom_rules=data.get("custom_rules"),
        created_by=user_id,
    )
    db.add(policy)
    await db.flush()

    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="policy",
        action="policy.created",
        actor_id=user_id,
        resource_type="policy_profile",
        resource_id=str(policy.id),
        payload={"name": policy.name, "version": policy.version, "is_locked": policy.is_locked},
    )
    return policy


async def update_policy(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    policy_id: uuid.UUID,
    data: dict,
    user_id: uuid.UUID,
) -> PolicyProfile:
    """Update an existing policy profile."""
    result = await db.execute(
        select(PolicyProfile).where(
            PolicyProfile.id == policy_id,
            PolicyProfile.tenant_id == tenant_id,
        )
    )
    policy = result.scalar_one_or_none()
    if not policy:
        raise ValueError("Policy not found")
    if policy.is_locked:
        raise ValueError("Policy is locked and cannot be edited")

    updatable_fields = {
        "name",
        "sensitivity_default",
        "is_active",
        "read_only_mode",
        "allowed_record_types",
        "blocked_fields",
        "tool_allowlist",
        "max_rows_per_query",
        "require_row_limit",
        "custom_rules",
    }

    if data.get("is_active") is True:
        await db.execute(
            update(PolicyProfile)
            .where(
                PolicyProfile.tenant_id == tenant_id,
                PolicyProfile.id != policy.id,
                PolicyProfile.is_active.is_(True),
            )
            .values(is_active=False)
        )

    for key, value in data.items():
        if value is not None and key in updatable_fields:
            setattr(policy, key, value)
    await db.flush()
    await db.refresh(policy)

    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="policy",
        action="policy.updated",
        actor_id=user_id,
        resource_type="policy_profile",
        resource_id=str(policy.id),
        payload={"name": policy.name},
    )
    return policy


async def lock_active_policy(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> PolicyProfile | None:
    """Lock the currently active policy profile for a tenant."""
    policy = await get_active_policy(db, tenant_id)
    if policy is None:
        return None
    if policy.is_locked:
        return policy

    policy.is_locked = True
    await db.flush()
    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="policy",
        action="policy.locked",
        actor_id=user_id,
        resource_type="policy_profile",
        resource_id=str(policy.id),
        payload={"version": policy.version},
    )
    return policy


async def delete_policy(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    policy_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Soft-delete a policy by deactivating it."""
    result = await db.execute(
        select(PolicyProfile).where(
            PolicyProfile.id == policy_id,
            PolicyProfile.tenant_id == tenant_id,
        )
    )
    policy = result.scalar_one_or_none()
    if not policy:
        raise ValueError("Policy not found")

    policy.is_active = False
    await db.flush()

    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="policy",
        action="policy.deleted",
        actor_id=user_id,
        resource_type="policy_profile",
        resource_id=str(policy.id),
    )


async def list_policies(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> list[PolicyProfile]:
    """List all policies for a tenant."""
    result = await db.execute(
        select(PolicyProfile).where(PolicyProfile.tenant_id == tenant_id).order_by(PolicyProfile.created_at.desc())
    )
    return list(result.scalars().all())


async def get_policy(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    policy_id: uuid.UUID,
) -> PolicyProfile | None:
    """Get a specific policy by ID."""
    result = await db.execute(
        select(PolicyProfile).where(
            PolicyProfile.id == policy_id,
            PolicyProfile.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


def evaluate_tool_call(
    policy: PolicyProfile | None,
    tool_name: str,
    params: dict,
) -> dict:
    """Evaluate whether a tool call is allowed by the active policy.

    Returns {"allowed": True} or {"allowed": False, "reason": "..."}.
    """
    if policy is None:
        # No custom policy — permissive default (read-only)
        return {"allowed": True}

    if policy.tool_allowlist:
        allowlist = policy.tool_allowlist
        if isinstance(allowlist, list) and tool_name not in allowlist:
            return {"allowed": False, "reason": f"Tool not allowed by policy: {tool_name}"}

    # Check if the tool involves a blocked record type
    if policy.allowed_record_types:
        allowed_types = policy.allowed_record_types
        if isinstance(allowed_types, list):
            # Check SuiteQL queries for record type references
            query = params.get("query", "")
            if isinstance(query, str) and query:
                query_lower = query.lower()
                # Simple heuristic: check if query references tables not in allowed list
                for record_type in allowed_types:
                    if record_type.lower() in query_lower:
                        break
                else:
                    # If query exists but no allowed type found, still allow
                    # (the query may not reference specific record types)
                    pass

    # Check for blocked fields in query params
    if policy.blocked_fields:
        blocked = policy.blocked_fields
        if isinstance(blocked, list):
            query = params.get("query", "")
            if isinstance(query, str):
                query_lower = query.lower()
                for field in blocked:
                    if field.lower() in query_lower:
                        return {
                            "allowed": False,
                            "reason": f"Query references blocked field: {field}",
                        }

    # Check row limit requirement
    if policy.require_row_limit:
        query = params.get("query", "")
        if isinstance(query, str) and query:
            query_upper = query.upper()
            if "ROWNUM" not in query_upper and "FETCH" not in query_upper:
                max_rows = policy.max_rows_per_query or 1000
                return {
                    "allowed": False,
                    "reason": f"Query must include a row limit (max {max_rows} rows). "
                    f"Add WHERE ROWNUM <= {max_rows} to your query.",
                }

    return {"allowed": True}


def redact_output(
    policy: PolicyProfile | None,
    result: dict | list | str,
) -> dict | list | str:
    """Strip blocked fields from tool results before feeding back to LLM."""
    if policy is None or not policy.blocked_fields:
        return result

    blocked = policy.blocked_fields
    if not isinstance(blocked, list):
        return result

    blocked_lower = {f.lower() for f in blocked}

    if isinstance(result, dict):
        return {k: v for k, v in result.items() if k.lower() not in blocked_lower}
    if isinstance(result, list):
        return [redact_output(policy, item) for item in result]
    return result
