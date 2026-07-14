"""Deterministic resolution planner — Phase 1 of the summary-first recon rework.

Pure Decimal rule engine: maps one ReconciliationResult's fields to a proposed
resolution (PlannedProposal) or None (skip). NO LLM, NO I/O in plan_result —
the async orchestrator (plan_run, below in this module) owns the DB.

Ordered rules (first match wins; spec mapping table):
  1. already posted in a prior run (guard)      → skip
  2. clean deterministic match, zero variance   → skip (never reaches proposals)
  3. chargeback / refund-shaped                 → needs_human (policy gate)
  4. evidence: matched deposit unapplied,
     variance_type != amount_mismatch           → apply_deposit
 4b. zero-variance fuzzy match, no evidence      → skip (approve-the-match; no proposal noise)
  5. duplicate                                  → void_duplicate
  6. fees                                       → book_fee_line
  7. missing / missing_in_netsuite:
       payout failed/canceled                     → needs_human (funds never settled)
       recent payout (<= RECENT_PAYOUT_LAG_DAYS)
         AND payout healthy or unknown             → carry_forward (sync-lag timing item)
       else + order ref known                    → create_and_apply_deposit
       else                                       → needs_human
 7b. amount_mismatch:
       fee-explained (within FEE_EXPLAIN_TOLERANCE of fee_amount,
         AND netsuite_amount < stripe_amount — a fee only ever lowers
         NetSuite, never raises it)               → book_fee_line
       else                                                        → delegates to rule 8 semantics
  8. fx_rounding (and amount_mismatch fallback): ≤ materiality → writeoff_je; above → needs_human
  9. timing                                     → carry_forward (no booking, ever)
 10. anything else (manual_adjustment, unknown) → needs_human

Materiality NEVER changes action selection except writeoff_je eligibility
(rules 8/7b-fallback); it only sets above_materiality, which gates one-click
bulk approval. `root_cause` on every path is always the raw `variance_type`
string (group keys stay honest to source data).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services.reconciliation.four_bucket_classifier import is_material

# Mirrors the payout classifier's fee-match tolerance.
FEE_EXPLAIN_TOLERANCE = Decimal("0.50")
# New operational threshold (no prior art in the codebase): NetSuite deposit
# sync runs nightly (02:00 UTC), so 7 days is a generous in-transit window;
# revisit against observed sync lag.
RECENT_PAYOUT_LAG_DAYS = 7
# Payout statuses under which a recent "missing" charge is plausibly just
# in-flight sync lag. The recency branch (rule 7) also allows payout_status
# is None (no payout row joined — enrichment couldn't determine health, so it
# must not be treated as proof the payout died).
HEALTHY_PAYOUT_STATUSES = frozenset({"paid", "pending", "in_transit"})

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


def _materiality_split(
    root_cause: str,
    abs_variance: Decimal,
    above: bool,
    explain: str,
    *,
    sub_materiality_narrative: str,
    above_materiality_narrative: str,
) -> PlannedProposal:
    """Shared materiality-split body for rule 8 (fx_rounding) and the rule 7b
    (amount_mismatch) fallback when no fee evidence explains the variance.
    Narrative text is caller-supplied so each root cause gets an honest
    description instead of amount_mismatch borrowing fx_rounding's wording.
    """
    if not above:
        return _mk(root_cause, ACTION_WRITEOFF_JE, f"{sub_materiality_narrative}{explain}", abs_variance, above)
    return _mk(root_cause, ACTION_NEEDS_HUMAN, f"{above_materiality_narrative}{explain}", abs_variance, above)


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
    fee_amount: Decimal | None = None,
    days_since_payout: int | None = None,
    payout_status: str | None = None,
) -> PlannedProposal | None:
    """Total, pure. Returns None only for rules 1-2/2b (skips)."""
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
    # 3. policy gate: never auto-propose a booking for a chargeback (beats
    #    evidence rules — a chargeback is never auto-applied regardless of
    #    what the evidence dict says)
    if variance_type == "chargeback":
        return _mk(
            "chargeback",
            ACTION_NEEDS_HUMAN,
            f"Chargeback/dispute — requires human review before any booking.{explain}",
            abs_variance,
            above,
        )
    # 4. evidence-based rules BEFORE variance-type dispatch. Excludes
    #    amount_mismatch: an amount discrepancy must resolve through the
    #    mismatch dispatch (rule 7b) first — applying a deposit whose amount
    #    is KNOWN to be wrong is never correct, even when it also happens to
    #    be sitting unapplied.
    if evidence.get("deposit_unapplied") is True and netsuite_amount is not None and variance_type != "amount_mismatch":
        return _mk(
            root,
            ACTION_APPLY_DEPOSIT,
            f"Deposit exists but is unapplied — apply it to the linked order.{explain}",
            abs_variance,
            above,
        )
    # 4b. zero-variance fuzzy match, no evidence — approve-the-match case, not
    #     a proposal; the classic rules-bucket bulk approve covers it (removes
    #     the manual_adjustment amt=0.00 noise group observed live). Must run
    #     AFTER rule 4 so a fuzzy zero-variance match that DOES carry
    #     deposit_unapplied evidence still produces apply_deposit instead of
    #     being silently dropped.
    # Matches four_bucket_classifier._has_variance: an empty-string
    # variance_type still counts as "has variance" there, so this skip must
    # not treat it as variance-free too — only variance_type is None (no
    # variance signal at all) qualifies for the approve-the-match skip.
    if match_type == "fuzzy" and variance_amount == Decimal("0") and variance_type is None:
        return None
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
    # 7. missing counterpart (order engine emits "missing_in_netsuite"; legacy
    #    rows may still carry the plain "missing" string — both route the same)
    if variance_type in ("missing", "missing_in_netsuite"):
        # A failed/canceled payout never settles — funds never arrived, so
        # this can never be sync-lag regardless of recency; check first so it
        # preempts both the recency carry_forward and create_and_apply below
        # (auto-creating a deposit for money that never landed would be wrong).
        if payout_status in ("failed", "canceled"):
            return _mk(
                variance_type,
                ACTION_NEEDS_HUMAN,
                f"Stripe payout failed or was canceled — funds never settled; investigate.{explain}",
                abs_variance,
                above,
            )
        if (
            days_since_payout is not None
            and days_since_payout <= RECENT_PAYOUT_LAG_DAYS
            and (payout_status is None or payout_status in HEALTHY_PAYOUT_STATUSES)
        ):
            return _mk(
                variance_type,
                ACTION_CARRY_FORWARD,
                "Charge settled recently — NetSuite deposit likely not yet synced; carry forward as a timing item."
                f"{explain}",
                abs_variance,
                above,
            )
        if evidence.get("order_reference"):
            return _mk(
                variance_type,
                ACTION_CREATE_AND_APPLY,
                f"Charge has no NetSuite deposit — create a customer deposit and apply it to the order.{explain}",
                stripe_amount if stripe_amount is not None else abs_variance,
                above,
            )
        return _mk(
            variance_type,
            ACTION_NEEDS_HUMAN,
            f"Charge has no NetSuite deposit and no order reference — needs investigation.{explain}",
            abs_variance,
            above,
        )
    # 7b. amount_mismatch: fee-explained first, else fall through to a
    #     materiality split with its own honest narrative — an amount
    #     mismatch is NOT fx/rounding, so it must not borrow that wording.
    if variance_type == "amount_mismatch":
        if (
            fee_amount is not None
            and fee_amount > 0
            and abs(abs_variance - fee_amount) <= FEE_EXPLAIN_TOLERANCE
            # A Stripe fee can only ever make NetSuite LOWER than Stripe —
            # never equal to or higher. Without this, a mismatch where
            # NetSuite is too HIGH would be misexplained as a fee.
            and stripe_amount is not None
            and netsuite_amount is not None
            and netsuite_amount < stripe_amount
        ):
            return _mk(
                "amount_mismatch",
                ACTION_BOOK_FEE_LINE,
                f"Variance matches the Stripe processing fee — book as a fee line on the payout's bank "
                f"deposit.{explain}",
                abs_variance,
                above,
            )
        return _materiality_split(
            "amount_mismatch",
            abs_variance,
            above,
            explain,
            sub_materiality_narrative="Small residual amount mismatch — aggregate write-off journal.",
            above_materiality_narrative="Amount mismatch above materiality — needs investigation.",
        )
    # 8. fx/rounding: sub-materiality write-off (flagged JE fallback); material → human
    if variance_type == "fx_rounding":
        return _materiality_split(
            "fx_rounding",
            abs_variance,
            above,
            explain,
            sub_materiality_narrative="Sub-materiality FX/rounding difference — aggregate write-off journal.",
            above_materiality_narrative="FX/rounding variance above materiality — needs investigation.",
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
from datetime import date, datetime, timezone

from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_INSERT_CHUNK = 5000  # Framework-scale runs are tens of thousands of lines


async def plan_run(db: AsyncSession, tenant_id, run_id) -> dict:
    """Plan every non-clean result of *run_id* into resolution proposals.

    Idempotent: existing 'proposed' PLANNER rows for the run are superseded
    first; decided rows (approved/posted/…) are never touched and their
    results are not re-planned. Human decisions are preserved across a
    re-plan: a 'proposed' source='human' override is never superseded (it IS
    the decision, not planner output), and 'rejected' is treated as decided
    for THIS run — re-planning must not resurrect a rejected result with a
    fresh identical proposal (rejection is run-scoped: protected here,
    re-derived only by a new run with a new run_id). A per-item mapping error
    abstains that item (needs_human path is total, so this only guards truly
    unexpected data). Planning failure must never fail the run — callers wrap
    this in try/except.

    Concurrent (re-)plans of the SAME run are serialized by a transaction-scoped
    Postgres advisory lock (below) — the second caller blocks until the first
    commits, then sees the first plan's rows as decided/superseded and no-ops
    cleanly instead of racing the supersede/read/insert steps and hitting the
    partial unique index with a raw IntegrityError.
    """
    from app.models.canonical import Payout, PayoutLine
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

    # Serialize concurrent (re-)plans of the same run: the second caller waits,
    # then sees the first plan's rows as decided/superseded and no-ops cleanly.
    # xact-scoped: released automatically at commit/rollback.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"plan_run:{rid}"},
    )

    mat_abs, mat_pct = await load_materiality(db, tid)

    # 1. supersede this run's undecided PLANNER proposals (re-plan safety; the
    #    partial unique index would otherwise reject the fresh insert). Never
    #    supersede a human override — it is itself the human's decision, not
    #    something a re-plan should discard.
    superseded_count = (
        await db.execute(
            update(ReconResolutionProposal)
            .where(
                ReconResolutionProposal.run_id == rid,
                ReconResolutionProposal.tenant_id == tid,
                ReconResolutionProposal.status == "proposed",
                ReconResolutionProposal.source != "human",
            )
            .values(status="superseded")
            .execution_options(synchronize_session=False)
        )
    ).rowcount

    # 2. results still holding an ACTIVE proposal (approved/posting/posted/
    #    post_failed) are decided — exclude them from re-planning. A
    #    surviving human-override row (protected in step 1, still 'proposed')
    #    is itself ACTIVE, so its result is excluded here too. 'rejected' is
    #    ALSO included: within this run a rejection is a standing decision —
    #    re-planning must not resurrect it with a fresh identical proposal. A
    #    future run (new run_id, new results) still plans fresh.
    decided_result_ids = select(ReconResolutionProposal.result_id).where(
        ReconResolutionProposal.run_id == rid,
        ReconResolutionProposal.tenant_id == tid,
        ReconResolutionProposal.status.in_((*ACTIVE_PROPOSAL_STATUSES, "rejected")),
    )

    # 3. column-only select (evidence included — planner reads order_reference).
    #    Loaded BEFORE the cross-run guard query below so the guard can be
    #    bounded to just this run's charge ids instead of scanning the whole
    #    tenant's history.
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

    # 3b. batched payout_line fee/recency enrichment: collect the payout_line
    #     ids referenced by evidence["charge_payout_line_id"] (uuid-parse
    #     defensively — malformed or absent ids are simply skipped, never
    #     crash the plan), then look them up in one tenant-scoped query per
    #     5000-chunk (Framework-scale runs are tens of thousands of lines).
    #     A line with no matching row (or no id at all) leaves both
    #     fee_amount and days_since_payout as None — fully backward-compatible
    #     with plan_result's pre-Task-2 behavior. Deduped via a set — many
    #     results in a run commonly share the same payout_line.
    payout_line_ids: set[_uuid.UUID] = set()
    for row in rows:
        raw_id = (row.evidence or {}).get("charge_payout_line_id")
        if not raw_id:
            continue
        try:
            payout_line_ids.add(_uuid.UUID(str(raw_id)))
        except ValueError:
            continue

    payout_line_id_list = sorted(payout_line_ids)
    payout_line_info: dict[_uuid.UUID, tuple[Decimal | None, date | None, str | None]] = {}
    for i in range(0, len(payout_line_id_list), _INSERT_CHUNK):
        chunk = payout_line_id_list[i : i + _INSERT_CHUNK]
        pl_rows = (
            await db.execute(
                select(PayoutLine.id, PayoutLine.fee, Payout.arrival_date, Payout.status)
                .outerjoin(Payout, (Payout.id == PayoutLine.payout_id) & (Payout.tenant_id == tid))
                .where(PayoutLine.tenant_id == tid, PayoutLine.id.in_(chunk))
            )
        ).all()
        for pl_id, fee, arrival_date, payout_status in pl_rows:
            payout_line_info[pl_id] = (fee, arrival_date, payout_status)

    today = datetime.now(timezone.utc).date()

    # 4. cross-run double-posting guard: of THIS run's charge ids, which are
    #    already decided or in-flight toward NetSuite anywhere in this
    #    tenant's history. 'approved'/'posting'/'post_failed' must guard too,
    #    not just 'posted' — a charge approved (or mid-post, or failed-post
    #    and awaiting retry) in run 1 must not get a second, independent
    #    proposal planned for it in run 2 before run 1's posting resolves.
    #    'proposed' (undecided) and 'rejected' deliberately do NOT guard —
    #    those are not commitments toward NetSuite. Bounded to charge_ids
    #    (not a full tenant-history scan) so cost is proportional to this
    #    run, not tenant lifetime volume; queried in chunks to keep the
    #    IN-list bounded, using the (tenant_id, charge_source_id) index.
    #    An APPROVED carry_forward is excluded too: it's an acknowledged
    #    timing item, never a posting commitment, so it must not permanently
    #    suppress a charge whose deposit never actually arrives — the next
    #    run needs to be free to re-propose it (e.g. create_and_apply_deposit
    #    once the recency window passes).
    charge_ids = sorted({(row.evidence or {}).get("charge_source_id") for row in rows} - {None})
    decided_charge_ids: set[str] = set()
    for i in range(0, len(charge_ids), _INSERT_CHUNK):
        chunk = charge_ids[i : i + _INSERT_CHUNK]
        decided_charge_ids.update(
            (
                await db.execute(
                    select(ReconResolutionProposal.charge_source_id).where(
                        ReconResolutionProposal.tenant_id == tid,
                        ReconResolutionProposal.status.in_(("approved", "posting", "posted", "post_failed")),
                        ReconResolutionProposal.action != ACTION_CARRY_FORWARD,
                        ReconResolutionProposal.charge_source_id.in_(chunk),
                    )
                )
            )
            .scalars()
            .all()
        )

    now = datetime.now(timezone.utc)
    to_insert: list[dict] = []
    skipped_guard = 0
    by_action: dict[str, int] = {}
    for row in rows:
        evidence = row.evidence or {}
        charge_source_id = evidence.get("charge_source_id")
        fee_amount: Decimal | None = None
        days_since_payout: int | None = None
        payout_status: str | None = None
        raw_pl_id = evidence.get("charge_payout_line_id")
        if raw_pl_id:
            try:
                pl_uuid = _uuid.UUID(str(raw_pl_id))
            except ValueError:
                pl_uuid = None
            if pl_uuid is not None and pl_uuid in payout_line_info:
                fee_amount, arrival_date, payout_status = payout_line_info[pl_uuid]
                if arrival_date is not None:
                    # clamp at 0: a future-dated arrival_date (clock skew,
                    # bad data) must read as "just arrived", not a negative
                    # day count that would slip past the recency guard.
                    days_since_payout = max(0, (today - arrival_date).days)
        planned = plan_result(
            match_type=row.match_type,
            variance_type=row.variance_type,
            variance_amount=row.variance_amount,
            stripe_amount=row.stripe_amount,
            netsuite_amount=row.netsuite_amount,
            currency=row.currency,
            variance_explanation=row.variance_explanation,
            evidence=evidence,
            already_posted=charge_source_id in decided_charge_ids if charge_source_id else False,
            materiality_abs=mat_abs,
            materiality_pct=mat_pct,
            fee_amount=fee_amount,
            days_since_payout=days_since_payout,
            payout_status=payout_status,
        )
        if planned is None:
            if charge_source_id in decided_charge_ids:
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

    # carry_forward is a per-run, re-evaluable acknowledgment — when a later
    # run re-plans a charge, prior cross-run carry_forward proposals for that
    # charge are superseded; exactly one live proposal thread per charge. This
    # is the other half of the decided_charge_ids carry_forward exemption
    # above: that exemption lets a charge be re-planned instead of being
    # permanently suppressed, and this closes the loop by retiring the old
    # carry_forward row instead of leaving it orphaned next to the fresh one.
    inserted_charge_ids = sorted({row["charge_source_id"] for row in to_insert if row["charge_source_id"]})
    carry_forward_superseded_count = 0
    for i in range(0, len(inserted_charge_ids), _INSERT_CHUNK):
        chunk = inserted_charge_ids[i : i + _INSERT_CHUNK]
        carry_forward_superseded_count += (
            await db.execute(
                update(ReconResolutionProposal)
                .where(
                    ReconResolutionProposal.tenant_id == tid,
                    ReconResolutionProposal.charge_source_id.in_(chunk),
                    ReconResolutionProposal.action == ACTION_CARRY_FORWARD,
                    ReconResolutionProposal.status.in_(("proposed", "approved")),
                    ReconResolutionProposal.run_id != rid,
                )
                .values(status="superseded")
                .execution_options(synchronize_session=False)
            )
        ).rowcount

    summary = {
        "planned_count": len(to_insert),
        "skipped_guard_count": skipped_guard,
        "superseded_count": superseded_count,
        "carry_forward_superseded_count": carry_forward_superseded_count,
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
