"""Retrieve tenant-specific learned rules for agent context injection.

Learned rules are business logic rules saved by users via the tenant_save_learned_rule
tool. They should ALWAYS be injected into the agent prompt regardless of context_need.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.tenant_learned_rule import TenantLearnedRule

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def retrieve_learned_rules(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> list[dict]:
    """Retrieve active learned rules for a tenant.

    Returns list of {"category": str, "description": str} dicts.
    """
    result = await db.execute(
        select(TenantLearnedRule)
        .where(
            TenantLearnedRule.tenant_id == tenant_id,
            TenantLearnedRule.is_active == True,  # noqa: E712
        )
        .order_by(TenantLearnedRule.created_at)
    )
    rules = result.scalars().all()

    # Instrumentation: log rule count + categories so we can see how many
    # rules are being dumped into every prompt. This function is NOT
    # currently query-aware — it returns ALL active rules for the tenant,
    # every request, regardless of topic. Known handicap vs Claude + MCP.
    if rules:
        categories: dict[str, int] = {}
        for r in rules:
            cat = r.rule_category or "general"
            categories[cat] = categories.get(cat, 0) + 1
        print(
            f"[LEARNED_RULES_RETRIEVAL] tenant={str(tenant_id)[:8]} "
            f"count={len(rules)} categories={categories}",
            flush=True,
        )

    return [
        {
            "category": rule.rule_category or "general",
            "description": rule.rule_description,
        }
        for rule in rules
    ]
