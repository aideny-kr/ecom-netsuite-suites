"""Order-level reconciliation job: charge → deposit matching."""

from __future__ import annotations

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


class OrderReconJob:
    """Orchestrates order-level reconciliation: charge → deposit matching."""

    def __init__(self, db: AsyncSession, tenant_id: str) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.engine = OrderMatchingEngine()

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

            # 4-5. Fetch deposits with order references
            deposits = await self._fetch_deposits(
                date_from=date_from,
                date_to=date_to,
                subsidiary_id=subsidiary_id,
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
            buckets = await self._store_results(run_id, candidates)

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
                    dispatch_resolution_agent(str(self.tenant_id), str(run_id))
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
        # Load this tenant's order-reference pattern once (NULL -> engine default).
        order_ref_pattern = await load_order_ref_pattern(self.db, self.tenant_id)

        p = aliased(Payout)
        stmt = (
            select(PayoutLine, p.arrival_date)
            .join(p, PayoutLine.payout_id == p.id)
            .where(
                PayoutLine.tenant_id == self.tenant_id,
                PayoutLine.line_type == "charge",
                p.arrival_date >= date_from - _DATE_BUFFER,
                p.arrival_date <= date_to + _DATE_BUFFER,
            )
        )

        if subsidiary_id:
            stmt = stmt.where(PayoutLine.subsidiary_id == subsidiary_id)

        result = await self.db.execute(stmt)
        rows = result.all()

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
    ) -> list[NSPaymentRecord]:
        """Fetch deposits from netsuite_postings (custdep + deposit).

        Uses ±5 day buffer. Sets order_reference from related_payout_id.
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
        rows = result.scalars().all()

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
            for r in rows
        ]

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
    ) -> list[str]:
        """Persist match candidates as ReconciliationResult rows.

        Returns the computed four-bucket classification for each candidate, in
        order, so the caller can roll up per-bucket counts onto the run.

        Key differences from payout-level:
        - payout_id is always NULL (order-level, not payout-level)
        - deposit_id set when matched
        - evidence contains charge_source_id, order_reference, charge_payout_line_id
        """
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
