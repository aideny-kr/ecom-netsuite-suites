"""Save a tenant-wide learned rule (admin-only).

Persists user instructions, preferences, and corrections as semantic
memory that gets injected into all future chat sessions for the tenant.
Non-admin users get a session-only acknowledgment.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def execute(
    params: dict[str, Any],
    context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict:
    """Persist a learned rule for the tenant.

    Parameters
    ----------
    params.rule_description : str
        The business rule or preference to remember.
    params.rule_category : str, optional
        Category tag (e.g., "output_preference", "status_mapping", "query_logic").
    """
    from app.core.dependencies import has_permission
    from app.models.tenant_learned_rule import TenantLearnedRule

    rule_description = (params.get("rule_description") or "").strip()
    if not rule_description:
        return {"error": "rule_description is required"}

    rule_category = (params.get("rule_category") or "general").strip()

    ctx = context or {}
    tenant_id_str = ctx.get("tenant_id", "")
    actor_id_str = ctx.get("actor_id", "")
    db: AsyncSession | None = ctx.get("db")

    if db is None:
        return {"error": "Database session not available"}

    try:
        tenant_id = uuid.UUID(tenant_id_str) if tenant_id_str else None
        actor_id = uuid.UUID(actor_id_str) if actor_id_str else None
    except (ValueError, TypeError):
        return {"error": "Invalid tenant_id or actor_id"}

    if not tenant_id or not actor_id:
        return {"error": "Missing tenant_id or actor_id"}

    # Admin gate: only users with tenant.manage can persist rules
    is_admin = await has_permission(db, actor_id, "tenant.manage")
    if not is_admin:
        return {
            "status": "session_only",
            "message": (
                "This preference has been noted for the current session. "
                "Only tenant administrators can save persistent rules that "
                "apply across all future sessions."
            ),
        }

    # Persist the rule
    rule = TenantLearnedRule(
        tenant_id=tenant_id,
        rule_category=rule_category,
        rule_description=rule_description,
        is_active=True,
        created_by=actor_id,
    )
    db.add(rule)
    await db.flush()

    logger.info(
        "learned_rule.saved",
        extra={
            "tenant_id": str(tenant_id),
            "actor_id": str(actor_id),
            "rule_id": str(rule.id),
            "category": rule_category,
        },
    )

    return {
        "status": "saved",
        "rule_id": str(rule.id),
        "message": ("Rule saved successfully. This will be applied to all future chat sessions for your organization."),
    }
