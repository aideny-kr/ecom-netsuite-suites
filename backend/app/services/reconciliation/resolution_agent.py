"""ResolutionAgent — Phase 2 of the summary-first recon rework.

Investigates planner abstentions (source='planner', action='needs_human',
status='proposed') with ONE forced-tool LLM classification call per item over
deterministically gathered DB context. Output is validated code-side (action
allowlist, materiality guard, numeric-token contract) and applied as a
supersede-and-insert (source='agent') under the same invariants as plan_run.
The agent NEVER writes to NetSuite and NEVER touches human/decided proposals.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reconciliation import ReconResolutionProposal

AGENT_ALLOWED_ACTIONS = frozenset(
    {"book_fee_line", "create_and_apply_deposit", "apply_deposit", "writeoff_je", "carry_forward", "needs_human"}
)
MAX_ITEMS_PER_RUN = 50
PER_ITEM_TIMEOUT_SECONDS = 45
AGENT_MAX_TOKENS = 1024


async def fetch_agent_eligible(
    db: AsyncSession,
    tenant_id,
    run_id,
    limit: int = MAX_ITEMS_PER_RUN,
) -> list[ReconResolutionProposal]:
    """Planner abstentions the agent may investigate, oldest first, capped."""
    P = ReconResolutionProposal
    return list(
        (
            await db.execute(
                select(P)
                .where(
                    P.tenant_id == tenant_id,
                    P.run_id == run_id,
                    P.source == "planner",
                    P.action == "needs_human",
                    P.status == "proposed",
                )
                .order_by(P.created_at.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
