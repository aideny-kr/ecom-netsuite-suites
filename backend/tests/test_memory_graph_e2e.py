"""Seeded-tenant e2e for the Tenant Memory Graph (T2 gate).

Drives the FULL chain against real services (only the LLM is mocked):
existing learning rows -> backfill distills pending concepts -> a human confirms
one + rejects another -> ONLY the confirmed concept reaches the agent prompt.

This exercises the trust gate (review_state) end-to-end. NOTE: it does NOT prove
production RLS behavior — the local DB role bypasses RLS, so the FORCE->ENABLE fix
is verified post-deploy via live smoke, not here.
"""

import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.models.tenant_learned_rule import TenantLearnedRule
from app.models.tenant_memory_concept import TenantMemoryConcept
from app.models.tenant_query_pattern import TenantQueryPattern
from app.services import tenant_memory_service as svc
from app.services.chat.agents.unified_agent import UnifiedAgent
from app.services.memory_graph_service import retrieve_confirmed_concepts


async def test_backfill_confirm_readloop_e2e(db, admin_user):
    user, _ = admin_user
    tenant_id = user.tenant_id

    # 1. Seed the tenant's existing learning rows.
    db.add(
        TenantLearnedRule(
            tenant_id=tenant_id,
            rule_category="term_definition",
            rule_description="net revenue excludes refunds",
            is_active=True,
        )
    )
    db.add(TenantQueryPattern(tenant_id=tenant_id, user_question="how many failed orders", working_sql="SELECT 1"))
    await db.flush()

    # 2. Backfill distills them into PENDING concepts (mock the LLM only).
    from app.workers.tasks import tenant_memory_extract_backfill as bf

    fake = [
        {
            "name": "Net Revenue",
            "concept_type": "definition",
            "plain_english_summary": "Revenue excluding refunds.",
            "edges": [],
            "confidence": 0.9,
            "source_ids": [],
        },
        {
            "name": "Failed Order",
            "concept_type": "definition",
            "plain_english_summary": "An order whose status is failed.",
            "edges": [],
            "confidence": 0.8,
            "source_ids": [],
        },
    ]
    with patch.object(bf, "extract_concepts", new=AsyncMock(return_value=fake)):
        await bf._extract(db, str(tenant_id), uuid.uuid4())
        await db.flush()

    concepts = (
        (await db.execute(select(TenantMemoryConcept).where(TenantMemoryConcept.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    assert len(concepts) == 2
    assert all(c.review_state == "pending" for c in concepts)

    # 3. Trust gate: while everything is pending, the read-loop injects NOTHING.
    assert await retrieve_confirmed_concepts(db, tenant_id) == []

    # 4. A human confirms ONE concept and rejects the other.
    net_rev = next(c for c in concepts if c.name == "Net Revenue")
    failed = next(c for c in concepts if c.name == "Failed Order")
    await svc.update_concept(db, tenant_id, net_rev.id, review_state="confirmed", confirmed_by=user.id)
    await svc.soft_reject_concept(db, tenant_id, failed.id)
    await db.flush()

    # 5. The read-loop returns ONLY the confirmed concept.
    injected = await retrieve_confirmed_concepts(db, tenant_id)
    assert [c["name"] for c in injected] == ["Net Revenue"]

    # 6. It renders into the agent system prompt; the rejected one never does.
    agent = UnifiedAgent(tenant_id=tenant_id, user_id=user.id, correlation_id="e2e")
    agent._context = {"memory_concepts": injected}
    prompt = agent.system_prompt
    assert "<tenant_memory>" in prompt
    assert "Net Revenue" in prompt
    assert "Failed Order" not in prompt

    # 7. Editing the confirmed concept's text de-confirms it -> drops out of injection
    #    until re-vetted (the de-confirm-on-edit trust fix).
    await svc.update_concept(db, tenant_id, net_rev.id, summary="silently changed wording")
    await db.flush()
    assert await retrieve_confirmed_concepts(db, tenant_id) == []
