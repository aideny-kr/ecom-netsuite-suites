"""Order-level reconciliation job: charge → deposit matching."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.canonical import NetsuitePosting, Payout, PayoutLine
from app.models.reconciliation import ReconciliationResult, ReconciliationRun
from app.schemas.order_reconciliation import (
    ChargeRecord,
    NSPaymentRecord,
    OrderMatchCandidate,
)
from app.schemas.reconciliation import ReconRunSummary
from app.services.reconciliation.confidence_engine import advisory_confidence
from app.services.reconciliation.four_bucket_classifier import (
    BUCKET_AUTO_CLASSIFICATIONS,
    BUCKET_MATCHES,
    BUCKET_NEEDS_REVIEW,
    BUCKET_RULES,
    classify,
)
from app.services.reconciliation.materiality import load_materiality
from app.services.reconciliation.order_matching_engine import OrderMatchingEngine
from app.services.reconciliation.order_ref import (
    extract_order_ref,
    load_order_ref_pattern,
)
from app.workers.tasks.recon_resolution_agent import dispatch_resolution_agent

logger = structlog.get_logger()

_DATE_BUFFER = timedelta(days=14)

# Ref-keyed deposit fetch (2026-07-19). Operator: "date doesn't really matter as
# much... order number is an important indicator; utilize the keys and
# dimensions of the data." Framework's session ledger (2026-07-19) measured
# ref-matched lag p50 -3d / p99 +15d, with a genuine >28d tail (147 pairs);
# 1,089/1,095 (99.5%) of a recent run's missing_in_netsuite charges had their
# counterpart in netsuite_postings outside the +/-14d windowed fetch. This is a
# generous SANITY bound (not a proximity/scoring window) that comfortably
# covers the measured tail — the order reference alone decides the match.
REF_MATCH_SANITY_DAYS = 90

# Cap per IN(...) batch for the ref-keyed query, so a run with an unusually
# large charge set never emits one enormous IN clause.
_REF_CHUNK_SIZE = 5000

# Washout rule (operator decision 2026-07-21, recorded verbatim in
# docs/superpowers/plans/2026-07-21-recon-washout-and-currency-truth.md):
# "washout = full refund within 7 days of the charge + no deposit ever
# booked". A charge whose same-ref refund(s) net it to |amount| < $0.01 within
# this many days of the charge is a canceled order refunded before it ever
# reached NetSuite — not a missing deposit. Permanent, not a recency/sync-lag
# hold.
#
# STRICT WINDOW NETTING (operator ruling, 2026-07-21): only refunds dated
# on or after the charge, and within WASHOUT_WINDOW_DAYS of it, count toward
# the net-zero test AT ALL. A same-ref refund landing outside that window —
# including one dated BEFORE the charge, which can't be refunding it — is a
# slow trickle or unrelated same-ref noise, not washout evidence — the order
# shipped and NetSuite has a booked deposit (reversed later via a credit
# memo + refund); it either matches normally or deserves human review, no
# matter how the FULL refund history nets out.
WASHOUT_WINDOW_DAYS = 7

# Fetch-volume guard (review finding 1 mitigation, 2026-07-21): the refund
# fetch in _fetch_refunds is bounded only by REF_MATCH_SANITY_DAYS, not
# date_from/date_to, so an arbitrary-window run could still pull an
# unexpectedly large row count. Cheap tripwire (log + warn) until SQL-side
# ref narrowing is ticketed — does not change fetch behavior.
_REFUND_FETCH_WARN_THRESHOLD = 20_000


def _washout_evidence(
    charge_amount: Decimal,
    charge_date: date,
    refunds: list[tuple[Decimal, date]],
) -> dict | None:
    """Return washout evidence for one charge's same-ref refund lines, or None.

    STRICT WINDOW NETTING (operator ruling, 2026-07-21): only refunds dated
    between ``charge_date`` and ``charge_date + WASHOUT_WINDOW_DAYS``
    (inclusive both ends) count toward the net-zero test AT ALL. A refund
    landing outside the window — including one dated BEFORE the charge,
    which can't be refunding it — is a slow trickle or unrelated same-ref
    noise, not washout evidence — the order shipped and NetSuite has a
    booked deposit (reversed later via a credit memo + refund); it either
    matches normally or deserves human review. ``refund_amount`` and
    ``net_after_refund`` are computed from within-window refunds only;
    ``refund_date`` is the latest within-window refund date — the date the
    refund completed (i.e. the date the within-window net actually hit
    zero), not the earliest. Decimal arithmetic throughout; stringified only
    for the JSONB evidence dict.
    """
    within_window = [
        (amount, refund_date)
        for amount, refund_date in refunds
        if 0 <= (refund_date - charge_date).days <= WASHOUT_WINDOW_DAYS
    ]
    if not within_window:
        return None

    total_refund_amount = sum((amount for amount, _ in within_window), Decimal("0"))
    net_after_refund = charge_amount + total_refund_amount
    if abs(net_after_refund) >= Decimal("0.01"):
        return None

    _, latest_date = max(within_window, key=lambda r: r[1])

    return {
        "washout": True,
        "refund_date": latest_date.isoformat(),
        "refund_amount": str(total_refund_amount),
        "net_after_refund": str(net_after_refund),
    }


_PATTERN_UNLOADED = object()


class OrderReconJob:
    """Orchestrates order-level reconciliation: charge → deposit matching."""

    def __init__(self, db: AsyncSession, tenant_id: str) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.engine = OrderMatchingEngine()
        # Cache for _load_order_ref_pattern_once — both _fetch_charges and
        # _fetch_refunds need this tenant's order_ref_pattern within the same
        # run(); caching avoids querying TenantConfig twice per run. A plain
        # None is a valid loaded value (no custom pattern -> engine default),
        # so a distinct sentinel marks "not loaded yet".
        self._order_ref_pattern: str | None | object = _PATTERN_UNLOADED

    async def _load_order_ref_pattern_once(self) -> str | None:
        """Load this tenant's order_ref_pattern once per job instance."""
        if self._order_ref_pattern is _PATTERN_UNLOADED:
            self._order_ref_pattern = await load_order_ref_pattern(self.db, self.tenant_id)
        return self._order_ref_pattern

    async def run(
        self,
        date_from: date,
        date_to: date,
        subsidiary_id: str | None = None,
        job_id: str | None = None,
    ) -> ReconRunSummary:
        """Execute a full order-level reconciliation run.

        1. Create run record
        2. Fetch charges from payout_lines (type='charge', date via payouts.arrival_date JOIN)
        3. Extract order_reference from description using extract_order_ref()
        4. Fetch deposits from netsuite_postings (custdep + deposit, ±5 day buffer)
        5. Set order_reference from related_payout_id
        6. Run engine.match(charges, deposits)
        7. Store results: payout_id=NULL, deposit_id set when matched, evidence={...}
        8. Return summary
        """
        # 1. Create run record
        run_id = uuid.uuid4()
        run = ReconciliationRun(
            id=run_id,
            tenant_id=self.tenant_id,
            job_id=uuid.UUID(job_id) if job_id else None,
            date_from=date_from,
            date_to=date_to,
            subsidiary_id=subsidiary_id,
            status="running",
            parameters={
                "match_level": "order",
                "subsidiary_id": subsidiary_id,
            },
        )
        self.db.add(run)
        await self.db.commit()

        try:
            # 2-3. Fetch charges and extract order references
            charges = await self._fetch_charges(
                date_from=date_from,
                date_to=date_to,
                subsidiary_id=subsidiary_id,
            )

            # 4-5. Fetch deposits with order references. The distinct non-null
            # order_references from THIS run's charges drive the ref-keyed pass
            # (see _fetch_deposits) — the order reference decides matching, not
            # the date window.
            order_references = {c.order_reference for c in charges if c.order_reference}
            deposits = await self._fetch_deposits(
                date_from=date_from,
                date_to=date_to,
                subsidiary_id=subsidiary_id,
                order_references=order_references,
            )

            # Washout evidence (Phase B Task 1): same-ref refund lines for
            # this run's charges. NEVER passed to the matching engine —
            # refunds only feed the washout evidence check in _store_results.
            refunds_by_ref = await self._fetch_refunds(
                date_from=date_from,
                date_to=date_to,
                subsidiary_id=subsidiary_id,
                order_references=order_references,
            )

            logger.info(
                "order_recon_job.data_fetched",
                run_id=str(run_id),
                charges=len(charges),
                deposits=len(deposits),
            )

            # 6. Run matching
            candidates = self.engine.match(charges, deposits)

            # 7. Store results (returns the computed bucket per candidate, in order)
            buckets = await self._store_results(run_id, candidates, refunds_by_ref=refunds_by_ref)

            # Compute summary
            matched = [c for c in candidates if c.match_type in ("deterministic", "fuzzy")]
            exceptions = [c for c in candidates if c.match_type == "exception"]
            unmatched = [c for c in candidates if c.match_type == "unmatched"]
            total_variance = sum(c.variance_amount for c in candidates)

            # Update run record
            run.status = "completed"
            run.total_payouts = len(charges)
            run.total_deposits = len(deposits)
            run.matched_count = len(matched)
            run.exception_count = len(exceptions)
            run.unmatched_count = len(unmatched)
            run.total_variance = total_variance
            # R2a: per-bucket rollup counts (from the persisted classification)
            run.matches_count = buckets.count(BUCKET_MATCHES)
            run.rules_count = buckets.count(BUCKET_RULES)
            run.auto_classifications_count = buckets.count(BUCKET_AUTO_CLASSIFICATIONS)
            run.needs_review_count = buckets.count(BUCKET_NEEDS_REVIEW)
            await self.db.commit()

            # Phase 1 (summary-first rework): derive resolution proposals.
            # Planning failure must never fail the run — the page offers retry
            # via POST /runs/{run_id}/plan-resolutions. plan_run makes writes
            # before its own commit, so a failure here must roll back first —
            # otherwise a later commit on this session could persist a partial
            # plan.
            try:
                from app.core.database import set_tenant_context
                from app.services.reconciliation.resolution_planner import plan_run

                # The finalize commit above clears the transaction-scoped
                # SET LOCAL app.current_tenant_id; re-establish it before
                # plan_run's INSERTs into the FORCE-RLS'd
                # recon_resolution_proposals table.
                await set_tenant_context(self.db, self.tenant_id)
                await plan_run(self.db, self.tenant_id, run_id)

                # Phase 2: dispatch the ResolutionAgent tail right after a
                # successful plan — fire-and-forget, flag-gated (reconciliation
                # AND recon_resolution_agent, both default OFF for the agent).
                from app.services.feature_flag_service import is_enabled

                if await is_enabled(self.db, self.tenant_id, "reconciliation") and await is_enabled(
                    self.db, self.tenant_id, "recon_resolution_agent"
                ):
                    # Off the event loop, same as the plan-resolutions endpoint's
                    # dispatch — send_task does blocking I/O (broker connection).
                    await asyncio.to_thread(dispatch_resolution_agent, str(self.tenant_id), str(run_id))
            except Exception:
                await self.db.rollback()
                logger.exception("resolution_planning_failed", run_id=str(run_id))

            match_rate = Decimal(len(matched)) / Decimal(len(charges)) * 100 if charges else Decimal("0")

            summary = ReconRunSummary(
                run_id=str(run_id),
                status="completed",
                total_payouts=len(charges),
                total_deposits=len(deposits),
                matched_count=len(matched),
                exception_count=len(exceptions),
                unmatched_count=len(unmatched),
                total_variance=total_variance,
                match_rate=match_rate.quantize(Decimal("0.01")),
            )

            logger.info(
                "order_recon_job.completed",
                run_id=str(run_id),
                match_rate=float(match_rate),
            )
            return summary

        except Exception as e:
            run.status = "failed"
            await self.db.commit()
            logger.error("order_recon_job.failed", run_id=str(run_id), error=str(e))
            raise

    async def _fetch_payout_lines(
        self,
        *,
        line_types: list[str],
        date_from: date,
        date_to: date,
        subsidiary_id: str | None = None,
    ) -> list[tuple[PayoutLine, date]]:
        """Shared Payout-join/tenant/date/subsidiary scaffold for both the
        charge fetch and the refund fetch — the two call sites differ only
        in ``line_types`` and their date bounds (the charge path passes
        ``_DATE_BUFFER``-widened dates; the refund path passes its own
        ``REF_MATCH_SANITY_DAYS``-widened dates), both already computed by
        the caller. Returns raw (PayoutLine, arrival_date) row tuples —
        record construction stays with each caller since it differs
        completely (ChargeRecord list vs ref-keyed refund grouping).
        """
        p = aliased(Payout)
        stmt = (
            select(PayoutLine, p.arrival_date)
            .join(p, PayoutLine.payout_id == p.id)
            .where(
                PayoutLine.tenant_id == self.tenant_id,
                PayoutLine.line_type.in_(line_types),
                p.arrival_date >= date_from,
                p.arrival_date <= date_to,
            )
        )

        if subsidiary_id:
            stmt = stmt.where(PayoutLine.subsidiary_id == subsidiary_id)

        result = await self.db.execute(stmt)
        return result.all()

    async def _fetch_charges(
        self,
        date_from: date,
        date_to: date,
        subsidiary_id: str | None = None,
    ) -> list[ChargeRecord]:
        """Fetch charges from payout_lines with line_type='charge'.

        Joins payouts to get arrival_date for date filtering and charge_date.
        Extracts order_reference from description using extract_order_ref() with
        this tenant's configured pattern (NULL -> engine default).
        """
        # Cached per job instance — _fetch_refunds needs the same pattern.
        order_ref_pattern = await self._load_order_ref_pattern_once()

        rows = await self._fetch_payout_lines(
            line_types=["charge"],
            date_from=date_from - _DATE_BUFFER,
            date_to=date_to + _DATE_BUFFER,
            subsidiary_id=subsidiary_id,
        )

        return [
            ChargeRecord(
                id=str(pl.id),
                source_id=pl.source_id,
                payout_line_id=str(pl.id),
                amount=pl.amount,
                fee=pl.fee,
                net=pl.net,
                currency=pl.currency,
                charge_date=arrival_date,
                description=pl.description,
                order_reference=extract_order_ref(pl.description, order_ref_pattern),
            )
            for pl, arrival_date in rows
        ]

    async def _fetch_deposits(
        self,
        date_from: date,
        date_to: date,
        subsidiary_id: str | None = None,
        order_references: set[str] | None = None,
    ) -> list[NSPaymentRecord]:
        """Fetch deposits from netsuite_postings (custdep + deposit).

        Two passes, unioned and deduped by id:
          1. Windowed (unchanged) — ±14 day buffer around [date_from, date_to].
             Still the sole source of candidates for tier-2 fuzzy matching
             (no-ref charges never contribute to ``order_references``, so this
             pass alone serves them, unaffected by the ref-keyed pass below).
          2. Ref-keyed (new) — when ``order_references`` is non-empty, ALSO
             fetch postings whose ``related_payout_id`` is in that set, bounded
             only by REF_MATCH_SANITY_DAYS on either side (a sanity cap, not a
             proximity window) so a charge in-window still matches a deposit
             NetSuite posts weeks later. ``related_payout_id`` normally stores
             the extracted order ref when the deposit links to a sales order
             (``netsuite_deposit_sync.sync_netsuite_deposits`` applies the same
             ``extract_order_ref`` this job uses for charges); rows that fall
             back to that sync's legacy payout-id-from-memo path hold a Stripe
             payout id instead, which never equals an order reference, so
             those rows simply won't be ref-fetched here — the same
             visibility gap that existed before this change. Chunked at
             ``_REF_CHUNK_SIZE`` refs per IN(...) batch.

        Sets order_reference from related_payout_id.
        """
        stmt = select(NetsuitePosting).where(
            NetsuitePosting.tenant_id == self.tenant_id,
            NetsuitePosting.record_type.in_(["deposit", "custdep"]),
            NetsuitePosting.transaction_date >= date_from - _DATE_BUFFER,
            NetsuitePosting.transaction_date <= date_to + _DATE_BUFFER,
        )

        if subsidiary_id:
            stmt = stmt.where(NetsuitePosting.subsidiary_id == subsidiary_id)

        result = await self.db.execute(stmt)
        postings_by_id = {r.id: r for r in result.scalars().all()}

        if order_references:
            refs = sorted(order_references)
            # Measured off the request date_from/date_to, not the +/-14d
            # buffered charge window above — a charge near the edge of that
            # buffer effectively gets ~14 fewer days of reach than the nominal
            # 90. That asymmetry is deliberate slack inside a generous sanity
            # bound, not an oversight.
            sanity_from = date_from - timedelta(days=REF_MATCH_SANITY_DAYS)
            sanity_to = date_to + timedelta(days=REF_MATCH_SANITY_DAYS)
            for i in range(0, len(refs), _REF_CHUNK_SIZE):
                chunk = refs[i : i + _REF_CHUNK_SIZE]
                ref_stmt = select(NetsuitePosting).where(
                    NetsuitePosting.tenant_id == self.tenant_id,
                    NetsuitePosting.record_type.in_(["deposit", "custdep"]),
                    NetsuitePosting.related_payout_id.in_(chunk),
                    NetsuitePosting.transaction_date >= sanity_from,
                    NetsuitePosting.transaction_date <= sanity_to,
                )
                if subsidiary_id:
                    ref_stmt = ref_stmt.where(NetsuitePosting.subsidiary_id == subsidiary_id)

                ref_result = await self.db.execute(ref_stmt)
                for r in ref_result.scalars().all():
                    postings_by_id[r.id] = r  # union, deduped by id

        return [
            NSPaymentRecord(
                id=str(r.id),
                netsuite_internal_id=r.netsuite_internal_id,
                amount=r.amount,
                currency=r.currency,
                transaction_date=r.transaction_date,
                record_type=r.record_type,
                memo=r.memo,
                order_reference=r.related_payout_id,
            )
            for r in postings_by_id.values()
        ]

    async def _fetch_refunds(
        self,
        date_from: date,
        date_to: date,
        subsidiary_id: str | None = None,
        order_references: set[str] | None = None,
    ) -> dict[str, list[tuple[Decimal, date]]]:
        """Fetch same-ref refund/payment_refund payout_lines for washout evidence.

        Feeds ONLY the washout evidence check in ``_store_results`` — refund
        lines never enter ``OrderMatchingEngine.match()`` as charges or
        deposits (they aren't ``ChargeRecord``/``NSPaymentRecord`` at all,
        just amount/date tuples keyed by ref).

        Unlike the ref-keyed deposit pass in ``_fetch_deposits`` (which
        filters via ``NetsuitePosting.related_payout_id.in_(chunk)`` — a
        column populated at sync time), ``payout_lines`` has no pre-extracted
        order-ref column. So this bounds by ``REF_MATCH_SANITY_DAYS`` (the
        same sanity-cap reasoning as the deposit pass, via the joined
        ``Payout.arrival_date``) and tenant/line_type at the SQL level, then
        extracts each row's ref in Python using the SAME per-tenant pattern
        machinery ``_fetch_charges`` uses (never a second extraction scheme),
        keeping only rows whose ref is in this run's charge
        ``order_references``.

        Returns a dict keyed by order_reference -> list of
        ``(amount, refund_date)`` tuples (amount negative per the grounded
        fact that Stripe refund/payment_refund lines are always negative).
        """
        if not order_references:
            return {}

        # Cached per job instance (_fetch_charges loads it first in run()) —
        # avoids a second TenantConfig query for the same tenant/run.
        order_ref_pattern = await self._load_order_ref_pattern_once()

        sanity_from = date_from - timedelta(days=REF_MATCH_SANITY_DAYS)
        sanity_to = date_to + timedelta(days=REF_MATCH_SANITY_DAYS)

        rows = await self._fetch_payout_lines(
            line_types=["refund", "payment_refund"],
            date_from=sanity_from,
            date_to=sanity_to,
            subsidiary_id=subsidiary_id,
        )

        # Fetch-volume guard (review finding 1 mitigation): cheap tripwire
        # until SQL-side ref narrowing is ticketed — see
        # _REFUND_FETCH_WARN_THRESHOLD.
        logger.info(
            "order_recon_job.refund_fetch_fetched",
            count=len(rows),
            window_days=REF_MATCH_SANITY_DAYS,
        )
        if len(rows) > _REFUND_FETCH_WARN_THRESHOLD:
            logger.warning(
                "order_recon_job.refund_fetch_large",
                count=len(rows),
                window_days=REF_MATCH_SANITY_DAYS,
            )

        refunds_by_ref: dict[str, list[tuple[Decimal, date]]] = {}
        for pl, arrival_date in rows:
            if arrival_date is None:
                continue
            ref = extract_order_ref(pl.description, order_ref_pattern)
            if ref is None or ref not in order_references:
                continue
            refunds_by_ref.setdefault(ref, []).append((pl.amount, arrival_date))

        return refunds_by_ref

    async def _load_materiality(self) -> tuple[Decimal, Decimal]:
        """Load this tenant's recon materiality thresholds (abs, pct).

        Delegates to the shared loader (single source of truth). Falls back to the
        $50 / 1% defaults when no TenantConfig row exists.
        """
        return await load_materiality(self.db, self.tenant_id)

    async def _store_results(
        self,
        run_id: uuid.UUID,
        candidates: list[OrderMatchCandidate],
        refunds_by_ref: dict[str, list[tuple[Decimal, date]]] | None = None,
    ) -> list[str]:
        """Persist match candidates as ReconciliationResult rows.

        Returns the computed four-bucket classification for each candidate, in
        order, so the caller can roll up per-bucket counts onto the run.

        Key differences from payout-level:
        - payout_id is always NULL (order-level, not payout-level)
        - deposit_id set when matched
        - evidence contains charge_source_id, order_reference, charge_payout_line_id

        ``refunds_by_ref`` (Phase B Task 1): when an unmatched charge's
        same-ref refunds satisfy the washout rule, evidence gains
        ``{washout, refund_date, refund_amount, net_after_refund}``. Evidence
        only — match_type/variance_type/bucket are untouched here.

        AMBIGUITY NEVER AUTO-MATCHES (gate finding [MAJOR], 2026-07-21;
        mirrors the set-to-set same-ref deposit pairing precedent in
        order_matching_engine.py's ``_match_same_ref_group``):
        ``refunds_by_ref`` is keyed by order_reference only, so when 2+ of
        this run's charges share one ref, each would otherwise
        independently net against the SAME refund list — double-counting a
        single refund as covering multiple charges. ``ref_charge_counts``
        counts every charge in this run (matched or not) per ref; washout
        evidence below is gated to refs with EXACTLY ONE charge.
        """
        refunds_by_ref = refunds_by_ref or {}
        ref_charge_counts: dict[str, int] = {}
        for candidate in candidates:
            ref = candidate.charge.order_reference
            if ref:
                ref_charge_counts[ref] = ref_charge_counts.get(ref, 0) + 1
        mat_abs, mat_pct = await self._load_materiality()
        buckets: list[str] = []
        for candidate in candidates:
            # Determine status based on confidence
            if candidate.match_type == "unmatched":
                status = "pending"
            elif candidate.confidence >= Decimal("0.95"):
                status = "auto_matched"
            elif candidate.confidence >= Decimal("0.75"):
                status = "suggested"
            else:
                status = "pending"

            # deposit_id from matched deposit
            deposit_id = None
            if candidate.deposit:
                deposit_id = uuid.UUID(candidate.deposit.id)

            # R2a: persist the four-bucket classification at write-time. The
            # materiality base is the gross charge amount (also stored below as
            # stripe_amount), matching the migration backfill's relative base.
            bucket = classify(
                candidate.match_type,
                candidate.variance_type,
                candidate.variance_amount,
                materiality_abs=mat_abs,
                materiality_pct=mat_pct,
                matched_amount=candidate.charge.amount,
            )
            if candidate.ambiguous_same_ref:
                # Ambiguous same-ref disambiguation always gets human review
                # — bucket override, mirrors the HITL materiality philosophy.
                # The engine's confidence cap (< 0.95) should already have
                # kept this out of auto_matched; assert rather than assume.
                assert status != "auto_matched", (
                    "ambiguous same-ref pick reached auto_matched — the "
                    "engine's confidence cap should have prevented this"
                )
                bucket = BUCKET_NEEDS_REVIEW
            buckets.append(bucket)

            # R2 advisory composite for the ``confidence`` column — the decoupling
            # contract lives in confidence_engine.advisory_confidence (``status``
            # above reads the engine ladder, never this value). Unmatched (no
            # deposit) keeps the engine value (0). candidate.charge.charge_date is
            # the payout arrival/settlement date (set from payouts.arrival_date in
            # _fetch_charges), so temporal_score measures arrival→deposit
            # proximity (advisory).
            persisted_confidence, confidence_signals = advisory_confidence(
                candidate.confidence,
                matched=candidate.deposit is not None,
                charge_amount=candidate.charge.amount,
                deposit_amount=candidate.deposit.amount if candidate.deposit else None,
                charge_date=candidate.charge.charge_date,
                deposit_date=candidate.deposit.transaction_date if candidate.deposit else None,
            )

            # Build evidence dict; attach R2 sub-scores when available.
            evidence = {
                "charge_source_id": candidate.charge.source_id,
                "order_reference": candidate.charge.order_reference,
                "charge_payout_line_id": candidate.charge.payout_line_id,
            }
            if confidence_signals is not None:
                evidence["confidence_signals"] = confidence_signals
            # Tier-1 same-ref evidence: only present when several deposits
            # shared this charge's order_reference group — see
            # OrderMatchingEngine._match_same_ref_group.
            if candidate.same_ref_deposit_ids:
                evidence["same_ref_deposit_ids"] = candidate.same_ref_deposit_ids
            if candidate.ambiguous_same_ref:
                evidence["ambiguous_same_ref"] = True
            # Washout evidence (Phase B Task 1): only unmatched charges (no
            # deposit ever booked) are eligible — a charge that matched a
            # deposit is not a washout, regardless of any same-ref refund.
            # Ambiguity gate (gate finding [MAJOR], 2026-07-21): also
            # requires this ref to belong to exactly one charge in the run —
            # see ref_charge_counts above.
            if (
                candidate.deposit is None
                and candidate.charge.order_reference
                and ref_charge_counts.get(candidate.charge.order_reference, 0) == 1
            ):
                washout = _washout_evidence(
                    candidate.charge.amount,
                    candidate.charge.charge_date,
                    refunds_by_ref.get(candidate.charge.order_reference, []),
                )
                if washout is not None:
                    evidence.update(washout)

            result = ReconciliationResult(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                run_id=run_id,
                payout_id=None,  # Always NULL for order-level
                deposit_id=deposit_id,
                match_type=candidate.match_type,
                confidence=persisted_confidence,
                status=status,
                bucket=bucket,
                stripe_amount=candidate.charge.amount,
                netsuite_amount=candidate.deposit.amount if candidate.deposit else None,
                variance_amount=candidate.variance_amount,
                variance_type=candidate.variance_type,
                variance_explanation=candidate.variance_explanation,
                currency=candidate.charge.currency,
                match_rule=candidate.match_rule,
                evidence=evidence,
            )
            self.db.add(result)

        await self.db.commit()
        return buckets
