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
 6b. washout (evidence: same-ref refund(s) net
       the charge to ~$0 within the 7-day
       window; order_recon_job Task 1)          → carry_forward, root_cause="washout"
                                                   (permanent — beats rule 7's
                                                   create_and_apply_deposit; the
                                                   chargeback gate above still wins)
  7. missing / missing_in_netsuite:
       payout failed/canceled                     → needs_human (funds never settled)
       payout pending/in_transit AND past the
         recency window                            → needs_human (still unsettled)
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
bulk approval. `root_cause` is the raw `variance_type` string on every path
EXCEPT rule 6b (washout): there `root_cause` is the fixed literal "washout",
not `variance_type` (which stays "missing_in_netsuite" on the underlying
result) — washout is derived from evidence, not from the raw variance
classification, so group keys for that one rule are not honest to
`variance_type` the way every other rule's are.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services.reconciliation.four_bucket_classifier import CLOSED_RUN_STATUSES, is_material

# Single source of truth for the washout window (operator decision
# 2026-07-21, recorded verbatim in
# docs/superpowers/plans/2026-07-21-recon-washout-and-currency-truth.md) —
# imported, not duplicated, so the planner's narrative text can never drift
# from the value order_recon_job actually applies when deciding
# evidence["washout"]. This planner never recomputes whether a charge IS a
# washout; it only trusts that evidence as already decided by
# order_recon_job's ref-keyed refund fetch. Safe at module level: order_recon_job
# imports resolution_planner only inside OrderReconJob.run() (deferred), never
# at its own module top level, so there is no import cycle.
from app.services.reconciliation.order_recon_job import WASHOUT_WINDOW_DAYS

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

# RECENCY HOLDS: only the rule-7 sync-lag carry_forwards (root_cause in this
# set) get the special cross-run lifecycle in plan_run — see the design note
# above the cross-run guard, below. These root_cause values are structurally
# unique to rule 7 (no other rule ever emits them), so the set alone fully
# identifies that branch; no need to also check action there.
RECENCY_HOLD_ROOT_CAUSES = ("missing", "missing_in_netsuite")

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
    # 6b. washout: order_recon_job's ref-keyed refund fetch (Task 1) attaches
    #     evidence["washout"] when this charge's same-ref refund(s) net it to
    #     ~$0 within WASHOUT_WINDOW_DAYS — a canceled order refunded before it
    #     ever reached NetSuite, not a missing deposit. Checked here, AFTER
    #     the chargeback gate (rule 3) and duplicate/fees (rules 5/6), so
    #     those stricter/more-specific signals still win if they were ever to
    #     co-occur with washout evidence (today they structurally can't: Task
    #     1 only attaches washout evidence to unmatched order-engine
    #     candidates, which always carry variance_type="missing_in_netsuite" —
    #     never "chargeback"/"duplicate"/"fees" — but the precedence is
    #     pinned here in case that ever changes). Must run BEFORE rule 7,
    #     which would otherwise route the same evidence to
    #     create_and_apply_deposit with zero refund signal — the wrong
    #     proposal this rule exists to prevent. root_cause="washout" is a
    #     fixed literal (not the raw variance_type, matching the chargeback
    #     gate's own style) and is deliberately excluded from
    #     RECENCY_HOLD_ROOT_CAUSES: a washout is permanent, not a "re-check
    #     next run" sync-lag hold, so it must behave as an ordinary standing
    #     decision once approved (see the RECENCY HOLDS design note above
    #     that set). The narrative is a fixed template — no {explain} suffix
    #     — with only the evidence-sourced refund_date interpolated; the
    #     window-days figure is a program constant (WASHOUT_WINDOW_DAYS,
    #     above), not an invented number.
    if evidence.get("washout") is True:
        return _mk(
            "washout",
            ACTION_CARRY_FORWARD,
            f"Stripe charge fully refunded on {evidence.get('refund_date')} within "
            f"{WASHOUT_WINDOW_DAYS} days; order canceled — no NetSuite booking required.",
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
        # Past the recency window, a payout still pending/in_transit is
        # unsettled — Stripe hasn't confirmed the funds landed, so this must
        # not fall through to create_and_apply_deposit (that would propose a
        # NetSuite deposit for money that may never arrive). Checked before
        # the recency branch below; inside the window pending/in_transit is
        # still plausibly sync-lag and stays carry_forward via that branch.
        if (
            payout_status in ("pending", "in_transit")
            and days_since_payout is not None
            and days_since_payout > RECENT_PAYOUT_LAG_DAYS
        ):
            return _mk(
                variance_type,
                ACTION_NEEDS_HUMAN,
                f"Stripe payout still unsettled after the sync-lag window — investigate before creating a "
                f"deposit.{explain}",
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

from sqlalchemy import insert, or_, select, text, update
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

    Recency holds (rule-7 sync-lag carry_forwards, root_cause in
    RECENCY_HOLD_ROOT_CAUSES) are the one exception to the decided/re-plan
    rule above: they never suppress a re-plan of their charge, and a fresh
    proposal for that charge supersedes the old hold (see the design note
    above the cross-run guard). Every other carry_forward — e.g. rule-9
    timing — is an ordinary standing decision under the paragraph above:
    approved ⇒ suppresses re-planning ⇒ never system-superseded.

    Concurrent (re-)plans of the SAME run are serialized by a transaction-scoped
    Postgres advisory lock (below) — the second caller blocks until the first
    commits, then sees the first plan's rows as decided/superseded and no-ops
    cleanly instead of racing the supersede/read/insert steps and hitting the
    partial unique index with a raw IntegrityError.
    """
    from app.models.audit import AuditEvent
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

    # Shared across this plan's own audit event AND every per-proposal
    # cross-run carry_forward supersede audit event below — lets a reader
    # trace "which plan superseded this old carry_forward" back to the
    # 'recon.resolution.planned' summary event for the same run.
    correlation_id = f"resolution-plan-{_uuid.uuid4().hex}"

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
    #
    #    RECENCY HOLDS: only the rule-7 sync-lag carry_forwards
    #    (action='carry_forward' AND root_cause IN RECENCY_HOLD_ROOT_CAUSES —
    #    structurally unique to that branch) have the special cross-run
    #    lifecycle: they never feed this suppression guard, and they ARE
    #    superseded when a later run re-plans the same charge (their meaning
    #    is "deposit probably in transit — re-check next run"; the UI
    #    acknowledgment is a per-period snooze, and either the deposit
    #    arrives → no new proposal, or it escalates →
    #    create_and_apply_deposit/needs_human). This lifecycle applies
    #    regardless of source: an agent-authored hold (source='agent',
    #    inserted by resolution_agent.apply_agent_proposal after
    #    investigating a needs_human abstention) is just as much a per-run
    #    snooze as a planner-authored one, so it is superseded the same way —
    #    only source='human' is exempt (see the supersede query below). Every
    #    OTHER carry_forward (rule-9 timing, root_cause='timing') is an
    #    ordinary standing decision: approved ⇒ feeds this guard ⇒
    #    suppresses, and is never system-superseded. Cross-run 'proposed'
    #    rows for other actions may coexist across runs — a pre-existing
    #    property, tracked separately.
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
                        or_(
                            ReconResolutionProposal.action != ACTION_CARRY_FORWARD,
                            ReconResolutionProposal.root_cause.notin_(RECENCY_HOLD_ROOT_CAUSES),
                        ),
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

    # Recency holds only (see the RECENCY HOLDS design note above the
    # cross-run guard, above): their meaning is "re-check next run", so a
    # fresh proposal for the same charge supersedes the old hold — exactly
    # one live recency-hold thread per charge. Timing carry_forwards
    # (root_cause='timing') are excluded via the root_cause filter below —
    # they are standing decisions and are never system-superseded.
    closed_run_ids = select(ReconciliationRun.id).where(
        ReconciliationRun.tenant_id == tid, ReconciliationRun.status.in_(CLOSED_RUN_STATUSES)
    )
    inserted_charge_ids = sorted({row["charge_source_id"] for row in to_insert if row["charge_source_id"]})
    recency_holds_superseded_count = 0
    for i in range(0, len(inserted_charge_ids), _INSERT_CHUNK):
        chunk = inserted_charge_ids[i : i + _INSERT_CHUNK]
        superseded_rows = (
            await db.execute(
                update(ReconResolutionProposal)
                .where(
                    ReconResolutionProposal.tenant_id == tid,
                    ReconResolutionProposal.charge_source_id.in_(chunk),
                    ReconResolutionProposal.action == ACTION_CARRY_FORWARD,
                    ReconResolutionProposal.root_cause.in_(RECENCY_HOLD_ROOT_CAUSES),
                    ReconResolutionProposal.status.in_(("proposed", "approved")),
                    ReconResolutionProposal.run_id != rid,
                    # the one-live-thread invariant bends for frozen periods —
                    # closed-period acknowledgments are immutable audit
                    # history; the new proposal still supersedes them
                    # *logically* by being the only live row.
                    ReconResolutionProposal.run_id.notin_(closed_run_ids),
                    # Never supersede a human override — it is itself the
                    # human's decision.
                    ReconResolutionProposal.source != "human",
                )
                .values(status="superseded")
                .returning(ReconResolutionProposal.id, ReconResolutionProposal.run_id)
                .execution_options(synchronize_session=False)
            )
        ).all()
        recency_holds_superseded_count += len(superseded_rows)
        if superseded_rows:
            # Per-proposal audit (mirrors group_actions.approve_group_core's
            # per-line insert): reversing an already-acknowledged — possibly
            # already-approved — decision from a prior run needs its own
            # audit trail, not just the aggregate count in the summary.
            await db.execute(
                insert(AuditEvent),
                [
                    {
                        "tenant_id": tid,
                        "actor_id": None,
                        "actor_type": "system",
                        "category": "reconciliation",
                        "action": "recon.resolution.recency_hold_superseded",
                        "resource_type": "recon_resolution_proposal",
                        "resource_id": str(prop_id),
                        "correlation_id": correlation_id,
                        "payload": {"superseding_run_id": str(rid), "prior_run_id": str(prior_run_id)},
                    }
                    for prop_id, prior_run_id in superseded_rows
                ],
            )

    summary = {
        "planned_count": len(to_insert),
        "skipped_guard_count": skipped_guard,
        "superseded_count": superseded_count,
        "recency_holds_superseded_count": recency_holds_superseded_count,
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
        correlation_id=correlation_id,
        payload=summary,
    )
    await db.commit()
    return summary
