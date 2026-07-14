"""ResolutionAgent — Phase 2 of the summary-first recon rework.

Investigates planner abstentions (source='planner', action='needs_human',
status='proposed') with ONE forced-tool LLM classification call per item over
deterministically gathered DB context. Output is validated code-side (action
allowlist, materiality guard, numeric-token contract) and applied as a
supersede-and-insert (source='agent') under the same invariants as plan_run.
The agent NEVER writes to NetSuite and NEVER touches human/decided proposals.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reconciliation import ReconciliationResult, ReconResolutionProposal
from app.services.reconciliation.four_bucket_classifier import TERMINAL_RESULT_STATUSES, is_material
from app.services.reconciliation.narrative_contract import narrative_respects_evidence
from app.services.reconciliation.resolution_planner import VEHICLE_BY_ACTION, group_key_for

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


# ---------------------------------------------------------------------------
# Deterministic context gathering (DB-only, read-only, tenant-scoped)
# ---------------------------------------------------------------------------

_CANDIDATE_POSTING_LIMIT = 5
_AMOUNT_TOLERANCE_PCT = Decimal("0.05")


def _flatten_values(obj) -> list[str]:
    """Every leaf value in a (possibly nested) dict/list, stringified."""
    values: list[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            values.extend(_flatten_values(v))
    elif isinstance(obj, list):
        for v in obj:
            values.extend(_flatten_values(v))
    elif obj is not None:
        values.append(str(obj))
    return values


async def gather_context(db: AsyncSession, tenant_id, proposal: ReconResolutionProposal) -> dict:
    """Deterministic, read-only, tenant-scoped context for one proposal.

    Result row fields + evidence + up to 5 candidate NetsuitePosting rows
    (amount within +/-5% of stripe_amount OR memo matching order_reference)
    + payout line detail if a charge_payout_line_id is present. Every value
    is stringified so the narrative-contract validator can flatten it.
    """
    from app.models.canonical import NetsuitePosting, PayoutLine

    result = (
        await db.execute(
            select(ReconciliationResult).where(
                ReconciliationResult.id == proposal.result_id,
                ReconciliationResult.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()

    evidence = dict((result.evidence if result else None) or proposal.evidence or {})
    order_reference = evidence.get("order_reference")
    stripe_amount = result.stripe_amount if result is not None else None

    context: dict = {
        "root_cause": proposal.root_cause,
        "planner_action": proposal.action,
        "planner_narrative": proposal.narrative,
        "proposed_amount": str(proposal.proposed_amount),
        "currency": proposal.currency,
        "above_materiality": str(proposal.above_materiality),
        "variance_type": result.variance_type if result is not None else None,
        "variance_amount": str(result.variance_amount) if result is not None else str(proposal.proposed_amount),
        "stripe_amount": str(result.stripe_amount) if result is not None and result.stripe_amount is not None else None,
        "netsuite_amount": str(result.netsuite_amount)
        if result is not None and result.netsuite_amount is not None
        else None,
        "variance_explanation": result.variance_explanation if result is not None else None,
        "evidence": {k: str(v) for k, v in evidence.items()},
    }

    conditions = []
    if stripe_amount is not None:
        # A negative stripe_amount (refund/chargeback) flips which bound is
        # smaller — min/max rather than raw lower/upper keeps BETWEEN's
        # low <= high invariant instead of silently matching zero rows.
        bound_a = stripe_amount * (Decimal("1") - _AMOUNT_TOLERANCE_PCT)
        bound_b = stripe_amount * (Decimal("1") + _AMOUNT_TOLERANCE_PCT)
        conditions.append(NetsuitePosting.amount.between(min(bound_a, bound_b), max(bound_a, bound_b)))
    if order_reference:
        conditions.append(NetsuitePosting.memo.ilike(f"%{order_reference}%"))

    candidate_postings: list[dict] = []
    if conditions:
        clause = conditions[0] if len(conditions) == 1 else or_(*conditions)
        rows = (
            (
                await db.execute(
                    select(NetsuitePosting)
                    .where(NetsuitePosting.tenant_id == tenant_id, clause)
                    .limit(_CANDIDATE_POSTING_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        candidate_postings = [
            {
                "record_type": p.record_type,
                "amount": str(p.amount),
                "currency": p.currency,
                "memo": p.memo or "",
                "netsuite_internal_id": p.netsuite_internal_id or "",
            }
            for p in rows
        ]
    context["candidate_postings"] = candidate_postings

    payout_line_id = evidence.get("charge_payout_line_id")
    if payout_line_id:
        try:
            pl_id = _uuid.UUID(str(payout_line_id))
        except ValueError:
            pl_id = None
        if pl_id is not None:
            pl = (
                await db.execute(select(PayoutLine).where(PayoutLine.id == pl_id, PayoutLine.tenant_id == tenant_id))
            ).scalar_one_or_none()
            if pl is not None:
                context["payout_line"] = {
                    "line_type": pl.line_type,
                    "amount": str(pl.amount),
                    "fee": str(pl.fee),
                    "net": str(pl.net),
                    "currency": pl.currency,
                    "description": pl.description or "",
                }

    return context


# ---------------------------------------------------------------------------
# Single forced-tool LLM classification call
# ---------------------------------------------------------------------------

CLASSIFY_TOOL = {
    "name": "classify_resolution",
    "description": "Classify one reconciliation exception into a resolution action.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(AGENT_ALLOWED_ACTIONS)},
            "narrative": {
                "type": "string",
                "description": (
                    "One-paragraph explanation. Use ONLY numbers that appear verbatim in the provided context."
                ),
            },
            "key_evidence": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["action", "narrative", "key_evidence"],
    },
}

_AGENT_SYSTEM = """You investigate one reconciliation exception a deterministic planner \
could not resolve on its own. You are given deterministically gathered context: the \
result's fields, its evidence, and candidate NetSuite postings — nothing else.

Classify the exception using ONLY the values in the provided context. Never invent, \
estimate, or round a number that is not present verbatim in the context. If the \
correct action is unclear from the context, choose needs_human rather than guessing.

You NEVER write to NetSuite. Your output is a proposal for human review, nothing more."""


async def classify_item(adapter, model: str, context: dict) -> dict:
    """One forced-tool LLM call; returns the classify_resolution tool-use input."""
    import json

    response = await adapter.create_message(
        model=model,
        max_tokens=AGENT_MAX_TOKENS,
        system=_AGENT_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(context, default=str)}],
        tools=[CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "classify_resolution"},
    )
    for block in response.tool_use_blocks:
        if block.name == "classify_resolution":
            return block.input
    raise ValueError("classify_resolution tool was not invoked")


# ---------------------------------------------------------------------------
# Code-side output validation (allowlist, materiality guard, numeric contract)
# ---------------------------------------------------------------------------


def _degraded(reason: str, key_evidence: list) -> dict:
    return {
        "action": "needs_human",
        "narrative": f"Agent output rejected ({reason}); needs investigation.",
        "key_evidence": key_evidence,
        "contract_violation": reason,
    }


def validate_output(out: dict, context: dict, materiality: tuple[Decimal, Decimal]) -> dict:
    """Enforce the action allowlist, writeoff_je sub-materiality-only rule, and
    the no-LLM-numbers narrative contract. Any violation degrades to needs_human
    with a contract_violation note for the caller's audit payload."""
    action = out.get("action")
    narrative = out.get("narrative") or ""
    key_evidence = out.get("key_evidence") or []

    if action not in AGENT_ALLOWED_ACTIONS:
        return _degraded(f"disallowed action '{action}'", key_evidence)

    # Chargeback policy pin: chargebacks always need a human regardless of what
    # the model classified them as — gather_context always sets root_cause
    # from the proposal, so this doesn't need a separate parameter.
    if context.get("root_cause") == "chargeback" and action != "needs_human":
        return _degraded("chargeback_policy", key_evidence)

    if action == "writeoff_je":
        mat_abs, mat_pct = materiality
        variance_amount = Decimal(context.get("variance_amount") or "0")
        stripe_amount_raw = context.get("stripe_amount")
        stripe_amount = Decimal(stripe_amount_raw) if stripe_amount_raw is not None else None
        if is_material(variance_amount, stripe_amount, mat_abs, mat_pct):
            return _degraded("writeoff_je above materiality", key_evidence)

    evidence_values = _flatten_values(context)
    if not narrative_respects_evidence(narrative, evidence_values):
        return _degraded("invented number in narrative", key_evidence)

    return {"action": action, "narrative": narrative, "key_evidence": key_evidence}


# ---------------------------------------------------------------------------
# Apply: supersede-then-insert, scoped to ONE row, one transaction per item
# ---------------------------------------------------------------------------


async def apply_agent_proposal(db: AsyncSession, proposal: ReconResolutionProposal, out: dict) -> bool:
    """Re-check eligibility, supersede the planner row, insert the agent row.

    Mirrors plan_run's supersede-then-insert but scoped to ONE proposal. Returns
    False (no-op, nothing written) if the planner row is no longer eligible
    (a human decided meanwhile). Commits on success. If the inserted row is a
    recency-hold carry_forward (action='carry_forward', root_cause in
    resolution_planner.RECENCY_HOLD_ROOT_CAUSES), it shares the planner's
    cross-run snooze lifecycle — a later plan_run supersedes it exactly like
    a planner-authored hold (see the RECENCY HOLDS design note in
    resolution_planner.plan_run).
    """
    # A result can go terminal independently of this proposal (e.g. locked via
    # the classic per-result approve path, or closed by a period freeze) while
    # its proposal row is still 'proposed' — mirrors the not_terminal_result
    # guard approve_group_core uses. Skip instead of superseding: otherwise the
    # planner row is replaced by an agent row for a result the agent never
    # actually gets to touch.
    result_status = (
        await db.execute(
            select(ReconciliationResult.status).where(
                ReconciliationResult.id == proposal.result_id,
                ReconciliationResult.tenant_id == proposal.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if result_status in TERMINAL_RESULT_STATUSES:
        return False

    result = await db.execute(
        update(ReconResolutionProposal)
        .where(
            ReconResolutionProposal.id == proposal.id,
            ReconResolutionProposal.status == "proposed",
        )
        .values(status="superseded")
        .execution_options(synchronize_session="fetch")
    )
    if result.rowcount == 0:
        return False

    action = out["action"]
    vehicle = VEHICLE_BY_ACTION[action]
    new_group_key = group_key_for(proposal.root_cause, action, vehicle)
    evidence = dict(proposal.evidence or {})
    evidence["agent_key_evidence"] = out.get("key_evidence") or []

    now = datetime.now(timezone.utc)
    db.add(
        ReconResolutionProposal(
            tenant_id=proposal.tenant_id,
            run_id=proposal.run_id,
            result_id=proposal.result_id,
            root_cause=proposal.root_cause,
            action=action,
            booking_vehicle=vehicle,
            group_key=new_group_key,
            source="agent",
            narrative=out["narrative"],
            evidence=evidence,
            proposed_amount=proposal.proposed_amount,
            currency=proposal.currency,
            above_materiality=proposal.above_materiality,
            status="proposed",
            charge_source_id=proposal.charge_source_id,
            created_at=now,
            updated_at=now,
        )
    )
    await db.commit()
    return True
