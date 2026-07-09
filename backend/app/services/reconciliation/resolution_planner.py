"""Deterministic resolution planner — Phase 1 of the summary-first recon rework.

Pure Decimal rule engine: maps one ReconciliationResult's fields to a proposed
resolution (PlannedProposal) or None (skip). NO LLM, NO I/O in plan_result —
the async orchestrator (plan_run, below in this module) owns the DB.

Ordered rules (first match wins; spec mapping table):
  1. already posted in a prior run (guard)      → skip
  2. clean deterministic match, zero variance   → skip (never reaches proposals)
  3. evidence: matched deposit unapplied        → apply_deposit
  4. chargeback / refund-shaped                 → needs_human (policy gate)
  5. duplicate                                  → void_duplicate
  6. fees                                       → book_fee_line
  7. missing + order ref known                  → create_and_apply_deposit
  8. fx_rounding: ≤ materiality → writeoff_je; above → needs_human
  9. timing                                     → carry_forward (no booking, ever)
 10. anything else (manual_adjustment, unknown) → needs_human

Materiality NEVER changes action selection except writeoff_je eligibility
(rule 8); it only sets above_materiality, which gates one-click bulk approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services.reconciliation.four_bucket_classifier import is_material

ACTION_BOOK_FEE_LINE = "book_fee_line"
ACTION_CREATE_AND_APPLY = "create_and_apply_deposit"
ACTION_APPLY_DEPOSIT = "apply_deposit"
ACTION_CREDIT_MEMO_REFUND = "credit_memo_refund"
ACTION_VOID_DUPLICATE = "void_duplicate"
ACTION_WRITEOFF_JE = "writeoff_je"
ACTION_CARRY_FORWARD = "carry_forward"
ACTION_NEEDS_HUMAN = "needs_human"

# Canonical booking vehicle per action (multi-write actions use the primary
# record; secondary records land in netsuite_record_refs at posting time).
VEHICLE_BY_ACTION: dict[str, str] = {
    ACTION_BOOK_FEE_LINE: "deposit",
    ACTION_CREATE_AND_APPLY: "customerdeposit",
    ACTION_APPLY_DEPOSIT: "depositapplication",
    ACTION_CREDIT_MEMO_REFUND: "creditmemo",
    ACTION_VOID_DUPLICATE: "customerdeposit",
    ACTION_WRITEOFF_JE: "journalentry",
    ACTION_CARRY_FORWARD: "none",
    ACTION_NEEDS_HUMAN: "none",
}


def group_key_for(root_cause: str, action: str, booking_vehicle: str) -> str:
    return f"{root_cause}:{action}:{booking_vehicle}"


@dataclass(frozen=True)
class PlannedProposal:
    root_cause: str
    action: str
    booking_vehicle: str
    group_key: str
    narrative: str
    proposed_amount: Decimal
    above_materiality: bool


def _mk(
    root_cause: str,
    action: str,
    narrative: str,
    proposed_amount: Decimal,
    above: bool,
) -> PlannedProposal:
    vehicle = VEHICLE_BY_ACTION[action]
    return PlannedProposal(
        root_cause=root_cause,
        action=action,
        booking_vehicle=vehicle,
        group_key=group_key_for(root_cause, action, vehicle),
        narrative=narrative,
        proposed_amount=proposed_amount,
        above_materiality=above,
    )


def plan_result(
    *,
    match_type: str,
    variance_type: str | None,
    variance_amount: Decimal,
    stripe_amount: Decimal | None,
    netsuite_amount: Decimal | None,
    currency: str,
    variance_explanation: str | None,
    evidence: dict | None,
    already_posted: bool,
    materiality_abs: Decimal,
    materiality_pct: Decimal,
) -> PlannedProposal | None:
    """Total, pure. Returns None only for rules 1-2 (skips)."""
    evidence = evidence or {}
    abs_variance = abs(variance_amount)
    above = is_material(variance_amount, stripe_amount, materiality_abs, materiality_pct)
    explain = f" {variance_explanation}" if variance_explanation else ""
    root = variance_type or ("missing" if match_type == "unmatched" else "manual_adjustment")

    # 1. cross-run double-posting guard
    if already_posted:
        return None
    # 2. clean match — nothing to resolve
    if match_type == "deterministic" and variance_type is None and variance_amount == Decimal("0"):
        return None
    # 3. evidence-based rules BEFORE variance-type dispatch
    if evidence.get("deposit_unapplied") is True and netsuite_amount is not None:
        return _mk(
            root,
            ACTION_APPLY_DEPOSIT,
            f"Deposit exists but is unapplied — apply it to the linked order.{explain}",
            abs_variance,
            above,
        )
    # 4. policy gate: never auto-propose a booking for a chargeback
    if variance_type == "chargeback":
        return _mk(
            "chargeback",
            ACTION_NEEDS_HUMAN,
            f"Chargeback/dispute — requires human review before any booking.{explain}",
            abs_variance,
            above,
        )
    # 5. duplicates: reverse via the same record type (pre-checks at posting time)
    if variance_type == "duplicate":
        return _mk(
            "duplicate",
            ACTION_VOID_DUPLICATE,
            f"Duplicate deposit — void/reverse the original customer deposit.{explain}",
            netsuite_amount if netsuite_amount is not None else abs_variance,
            above,
        )
    # 6. fees: fee line on the payout's bank deposit (aggregated per payout at posting)
    if variance_type == "fees":
        return _mk(
            "fees",
            ACTION_BOOK_FEE_LINE,
            f"Stripe processing fee — book as a fee line on the payout's bank deposit.{explain}",
            abs_variance,
            above,
        )
    # 7. missing counterpart
    if variance_type == "missing":
        if evidence.get("order_reference"):
            return _mk(
                "missing",
                ACTION_CREATE_AND_APPLY,
                f"Charge has no NetSuite deposit — create a customer deposit and apply it to the order.{explain}",
                stripe_amount if stripe_amount is not None else abs_variance,
                above,
            )
        return _mk(
            "missing",
            ACTION_NEEDS_HUMAN,
            f"Charge has no NetSuite deposit and no order reference — needs investigation.{explain}",
            abs_variance,
            above,
        )
    # 8. fx/rounding: sub-materiality write-off (flagged JE fallback); material → human
    if variance_type == "fx_rounding":
        if not above:
            return _mk(
                "fx_rounding",
                ACTION_WRITEOFF_JE,
                f"Sub-materiality FX/rounding difference — aggregate write-off journal.{explain}",
                abs_variance,
                above,
            )
        return _mk(
            "fx_rounding",
            ACTION_NEEDS_HUMAN,
            f"FX/rounding variance above materiality — needs investigation.{explain}",
            abs_variance,
            above,
        )
    # 9. timing: reconciling item, never force-matched, never booked
    if variance_type == "timing":
        return _mk(
            "timing",
            ACTION_CARRY_FORWARD,
            f"Timing difference — carry forward as a reconciling item; no booking.{explain}",
            abs_variance,
            above,
        )
    # 10. everything else — the agent tail (Phase 2) / human
    return _mk(
        root,
        ACTION_NEEDS_HUMAN,
        f"Unexplained variance — needs investigation.{explain}",
        abs_variance,
        above,
    )


import logging
import uuid as _uuid
from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_INSERT_CHUNK = 5000  # Framework-scale runs are tens of thousands of lines


async def plan_run(db: AsyncSession, tenant_id, run_id) -> dict:
    """Plan every non-clean result of *run_id* into resolution proposals.

    Idempotent: existing 'proposed' rows for the run are superseded first;
    decided rows (approved/posted/…) are never touched and their results are
    not re-planned. A per-item mapping error abstains that item (needs_human
    path is total, so this only guards truly unexpected data). Planning failure
    must never fail the run — callers wrap this in try/except.
    """
    from app.models.reconciliation import (
        ACTIVE_PROPOSAL_STATUSES,
        ReconciliationResult,
        ReconciliationRun,
        ReconResolutionProposal,
    )
    from app.services import audit_service
    from app.services.reconciliation.materiality import load_materiality

    tid = tenant_id if isinstance(tenant_id, _uuid.UUID) else _uuid.UUID(str(tenant_id))
    rid = run_id if isinstance(run_id, _uuid.UUID) else _uuid.UUID(str(run_id))

    run = (
        await db.execute(
            select(ReconciliationRun).where(ReconciliationRun.id == rid, ReconciliationRun.tenant_id == tid)
        )
    ).scalar_one_or_none()
    if run is None:
        raise ValueError("run not found")

    mat_abs, mat_pct = await load_materiality(db, tid)

    # 1. supersede this run's undecided proposals (re-plan safety; the partial
    #    unique index would otherwise reject the fresh insert).
    superseded_count = (
        await db.execute(
            update(ReconResolutionProposal)
            .where(
                ReconResolutionProposal.run_id == rid,
                ReconResolutionProposal.tenant_id == tid,
                ReconResolutionProposal.status == "proposed",
            )
            .values(status="superseded")
            .execution_options(synchronize_session=False)
        )
    ).rowcount

    # 2. results still holding an ACTIVE proposal (approved/posting/posted/
    #    post_failed) are decided — exclude them from re-planning.
    decided_result_ids = select(ReconResolutionProposal.result_id).where(
        ReconResolutionProposal.run_id == rid,
        ReconResolutionProposal.tenant_id == tid,
        ReconResolutionProposal.status.in_(ACTIVE_PROPOSAL_STATUSES),
    )

    # 3. cross-run double-posting guard: charge ids with a posted proposal
    #    anywhere in this tenant's history.
    posted_charge_ids = set(
        (
            await db.execute(
                select(ReconResolutionProposal.charge_source_id).where(
                    ReconResolutionProposal.tenant_id == tid,
                    ReconResolutionProposal.status == "posted",
                    ReconResolutionProposal.charge_source_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    # 4. column-only select (evidence included — planner reads order_reference).
    rows = (
        await db.execute(
            select(
                ReconciliationResult.id,
                ReconciliationResult.match_type,
                ReconciliationResult.variance_type,
                ReconciliationResult.variance_amount,
                ReconciliationResult.stripe_amount,
                ReconciliationResult.netsuite_amount,
                ReconciliationResult.currency,
                ReconciliationResult.variance_explanation,
                ReconciliationResult.evidence,
            ).where(
                ReconciliationResult.run_id == rid,
                ReconciliationResult.tenant_id == tid,
                ReconciliationResult.bucket != "matches",
                ReconciliationResult.id.notin_(decided_result_ids),
            )
        )
    ).all()

    now = datetime.now(timezone.utc)
    to_insert: list[dict] = []
    skipped_guard = 0
    by_action: dict[str, int] = {}
    for row in rows:
        evidence = row.evidence or {}
        charge_source_id = evidence.get("charge_source_id")
        planned = plan_result(
            match_type=row.match_type,
            variance_type=row.variance_type,
            variance_amount=row.variance_amount,
            stripe_amount=row.stripe_amount,
            netsuite_amount=row.netsuite_amount,
            currency=row.currency,
            variance_explanation=row.variance_explanation,
            evidence=evidence,
            already_posted=charge_source_id in posted_charge_ids if charge_source_id else False,
            materiality_abs=mat_abs,
            materiality_pct=mat_pct,
        )
        if planned is None:
            if charge_source_id in posted_charge_ids:
                skipped_guard += 1
            continue
        by_action[planned.action] = by_action.get(planned.action, 0) + 1
        to_insert.append(
            {
                "tenant_id": tid,
                "run_id": rid,
                "result_id": row.id,
                "root_cause": planned.root_cause,
                "action": planned.action,
                "booking_vehicle": planned.booking_vehicle,
                "group_key": planned.group_key,
                "source": "planner",
                "narrative": planned.narrative,
                "evidence": {"charge_source_id": charge_source_id} if charge_source_id else None,
                "proposed_amount": planned.proposed_amount,
                "currency": row.currency,
                "above_materiality": planned.above_materiality,
                "status": "proposed",
                "charge_source_id": charge_source_id,
                "created_at": now,
                "updated_at": now,
            }
        )

    for i in range(0, len(to_insert), _INSERT_CHUNK):
        await db.execute(insert(ReconResolutionProposal), to_insert[i : i + _INSERT_CHUNK])

    summary = {
        "planned_count": len(to_insert),
        "skipped_guard_count": skipped_guard,
        "superseded_count": superseded_count,
        "by_action": by_action,
    }
    await audit_service.log_event(
        db=db,
        tenant_id=tid,
        category="reconciliation",
        action="recon.resolution.planned",
        actor_id=None,
        actor_type="system",
        resource_type="reconciliation_run",
        resource_id=str(rid),
        correlation_id=f"resolution-plan-{_uuid.uuid4().hex}",
        payload=summary,
    )
    await db.commit()
    return summary
