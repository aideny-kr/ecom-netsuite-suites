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
    query_text: str | None = None,
    max_rules: int = 10,
) -> list[dict]:
    """Retrieve active learned rules for a tenant, optionally filtered by query relevance.

    When `query_text` is provided, rules are ranked by keyword overlap
    with the query and only the top `max_rules` are returned. Rules
    tagged with category "status_mapping" or "term_definition" are
    always included (they're short, universally useful reference data).

    When `query_text` is None, returns ALL active rules (backwards compat
    for callers that haven't been updated yet).

    Returns list of {"category": str, "description": str} dicts.
    """
    import re

    result = await db.execute(
        select(TenantLearnedRule)
        .where(
            TenantLearnedRule.tenant_id == tenant_id,
            TenantLearnedRule.is_active == True,  # noqa: E712
        )
        .order_by(TenantLearnedRule.created_at)
    )
    all_rules = result.scalars().all()

    if not all_rules:
        return []

    # If no query text, return all (backwards compat)
    if not query_text:
        print(
            f"[LEARNED_RULES_RETRIEVAL] tenant={str(tenant_id)[:8]} "
            f"count={len(all_rules)} (no query filter, returning all)",
            flush=True,
        )
        return [{"category": r.rule_category or "general", "description": r.rule_description} for r in all_rules]

    # Query-aware filtering: extract keywords, rank rules by overlap
    query_words = set(re.findall(r"\b\w{3,}\b", query_text.lower()))

    # Categories that are always included (short, universal reference)
    _ALWAYS_INCLUDE_CATEGORIES = {"status_mapping", "term_definition"}

    scored_rules: list[tuple[int, object]] = []
    always_rules: list[object] = []

    for rule in all_rules:
        cat = rule.rule_category or "general"
        if cat in _ALWAYS_INCLUDE_CATEGORIES:
            always_rules.append(rule)
            continue

        desc_lower = (rule.rule_description or "").lower()
        hits = sum(1 for w in query_words if w in desc_lower)
        scored_rules.append((hits, rule))

    # Sort by relevance, take top N
    scored_rules.sort(key=lambda x: x[0], reverse=True)
    relevant = [r for hits, r in scored_rules[:max_rules] if hits > 0]

    # Combine: always-include + relevant
    selected = always_rules + relevant

    # Instrumentation
    total = len(all_rules)
    returned = len(selected)
    always_count = len(always_rules)
    relevant_count = len(relevant)
    print(
        f"[LEARNED_RULES_RETRIEVAL] tenant={str(tenant_id)[:8]} "
        f"total={total} returned={returned} "
        f"(always={always_count} relevant={relevant_count}/{total - always_count}) "
        f'query="{query_text[:60]}"',
        flush=True,
    )

    return [{"category": r.rule_category or "general", "description": r.rule_description} for r in selected]
