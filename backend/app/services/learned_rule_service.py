"""CRUD for tenant learned rules (semantic memory injected into the chat agent).

All operations are tenant-scoped. Mutations flush but do NOT commit — the caller
(endpoint) commits and audit-logs, per the FastAPI/SQLAlchemy convention.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant_learned_rule import TenantLearnedRule


async def list_rules(db: AsyncSession, tenant_id: uuid.UUID) -> list[TenantLearnedRule]:
    """All rules for a tenant (active + inactive), newest first."""
    result = await db.execute(
        select(TenantLearnedRule)
        .where(TenantLearnedRule.tenant_id == tenant_id)
        .order_by(TenantLearnedRule.created_at.desc())
    )
    return list(result.scalars().all())


async def get_rule(db: AsyncSession, tenant_id: uuid.UUID, rule_id: uuid.UUID) -> TenantLearnedRule | None:
    """Fetch a single rule, scoped to the tenant (None if missing or cross-tenant)."""
    result = await db.execute(
        select(TenantLearnedRule).where(
            TenantLearnedRule.id == rule_id,
            TenantLearnedRule.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def create_rule(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    rule_description: str,
    rule_category: str | None,
    created_by: uuid.UUID | None,
) -> TenantLearnedRule:
    rule = TenantLearnedRule(
        tenant_id=tenant_id,
        rule_description=rule_description,
        rule_category=(rule_category or "general"),
        is_active=True,
        created_by=created_by,
    )
    db.add(rule)
    await db.flush()
    return rule


async def update_rule(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    rule_id: uuid.UUID,
    *,
    rule_description: str | None = None,
    rule_category: str | None = None,
    is_active: bool | None = None,
) -> TenantLearnedRule | None:
    """Patch the provided fields in place. Returns None if the rule isn't found."""
    rule = await get_rule(db, tenant_id, rule_id)
    if rule is None:
        return None
    if rule_description is not None:
        rule.rule_description = rule_description
    if rule_category is not None:
        rule.rule_category = rule_category
    if is_active is not None:
        rule.is_active = is_active
    await db.flush()
    return rule


async def delete_rule(db: AsyncSession, tenant_id: uuid.UUID, rule_id: uuid.UUID) -> bool:
    """Hard-delete a rule. Returns False if it isn't found for this tenant."""
    rule = await get_rule(db, tenant_id, rule_id)
    if rule is None:
        return False
    await db.delete(rule)
    await db.flush()
    return True
