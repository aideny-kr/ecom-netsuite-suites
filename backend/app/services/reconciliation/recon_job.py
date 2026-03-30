"""Reconciliation job orchestrator: fetch -> match -> classify -> store."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import NetsuitePosting, Payout
from app.models.reconciliation import ReconciliationResult, ReconciliationRun
from app.schemas.reconciliation import (
    DepositRecord,
    MatchCandidate,
    PayoutRecord,
    ReconRunSummary,
)
from app.services.reconciliation.matching_engine import MatchingEngine

logger = structlog.get_logger()


class ReconJobRunner:
    """Orchestrates a single reconciliation run."""

    def __init__(self, db: AsyncSession, tenant_id: str) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.engine = MatchingEngine()

    async def run(
        self,
        date_from: date,
        date_to: date,
        subsidiary_id: str | None = None,
        payout_ids: list[str] | None = None,
        job_id: str | None = None,
    ) -> ReconRunSummary:
        """Execute a full reconciliation run.

        1. Create run record
        2. Fetch payouts from canonical tables
        3. Fetch deposits from netsuite_postings
        4. Run matching engine
        5. Store results
        6. Update run summary
        """
        # Create run record
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
                "payout_ids": payout_ids,
                "subsidiary_id": subsidiary_id,
            },
        )
        self.db.add(run)
        await self.db.commit()

        try:
            # Fetch data
            payouts = await self._fetch_payouts(
                date_from=date_from,
                date_to=date_to,
                subsidiary_id=subsidiary_id,
                payout_ids=payout_ids,
            )
            deposits = await self._fetch_deposits(
                date_from=date_from,
                date_to=date_to,
                subsidiary_id=subsidiary_id,
            )

            logger.info(
                "recon_job.data_fetched",
                run_id=str(run_id),
                payouts=len(payouts),
                deposits=len(deposits),
            )

            # Run matching
            candidates = self.engine.match(payouts, deposits)

            # Store results
            await self._store_results(run_id, candidates)

            # Compute summary
            matched = [c for c in candidates if c.match_type in ("deterministic", "fuzzy")]
            exceptions = [c for c in candidates if c.match_type == "exception"]
            unmatched = [c for c in candidates if c.match_type == "unmatched"]
            total_variance = sum(c.variance_amount for c in candidates)

            # Update run record
            run.status = "completed"
            run.total_payouts = len(payouts)
            run.total_deposits = len(deposits)
            run.matched_count = len(matched)
            run.exception_count = len(exceptions)
            run.unmatched_count = len(unmatched)
            run.total_variance = total_variance
            await self.db.commit()

            match_rate = Decimal(len(matched)) / Decimal(len(payouts)) * 100 if payouts else Decimal("0")

            summary = ReconRunSummary(
                run_id=str(run_id),
                status="completed",
                total_payouts=len(payouts),
                total_deposits=len(deposits),
                matched_count=len(matched),
                exception_count=len(exceptions),
                unmatched_count=len(unmatched),
                total_variance=total_variance,
                match_rate=match_rate.quantize(Decimal("0.01")),
            )

            logger.info("recon_job.completed", run_id=str(run_id), match_rate=float(match_rate))
            return summary

        except Exception as e:
            run.status = "failed"
            await self.db.commit()
            logger.error("recon_job.failed", run_id=str(run_id), error=str(e))
            raise

    async def _fetch_payouts(
        self,
        date_from: date,
        date_to: date,
        subsidiary_id: str | None = None,
        payout_ids: list[str] | None = None,
    ) -> list[PayoutRecord]:
        """Fetch payouts from canonical table for the given period."""
        stmt = select(Payout).where(
            Payout.tenant_id == self.tenant_id,
            Payout.status == "paid",
        )

        if payout_ids:
            stmt = stmt.where(Payout.source_id.in_(payout_ids))
        else:
            stmt = stmt.where(
                Payout.arrival_date >= date_from,
                Payout.arrival_date <= date_to,
            )

        if subsidiary_id:
            stmt = stmt.where(Payout.subsidiary_id == subsidiary_id)

        result = await self.db.execute(stmt)
        rows = result.scalars().all()

        return [
            PayoutRecord(
                id=str(r.id),
                source_id=r.source_id,
                amount=r.amount,
                net_amount=r.net_amount,
                fee_amount=r.fee_amount,
                currency=r.currency,
                arrival_date=r.arrival_date,
                subsidiary_id=r.subsidiary_id,
            )
            for r in rows
        ]

    async def _fetch_deposits(
        self,
        date_from: date,
        date_to: date,
        subsidiary_id: str | None = None,
    ) -> list[DepositRecord]:
        """Fetch bank deposits from netsuite_postings for the given period."""
        stmt = select(NetsuitePosting).where(
            NetsuitePosting.tenant_id == self.tenant_id,
            NetsuitePosting.record_type.in_(["deposit", "bankdeposit", "journalentry"]),
            NetsuitePosting.transaction_date >= date_from,
            NetsuitePosting.transaction_date <= date_to,
        )

        if subsidiary_id:
            stmt = stmt.where(NetsuitePosting.subsidiary_id == subsidiary_id)

        result = await self.db.execute(stmt)
        rows = result.scalars().all()

        return [
            DepositRecord(
                id=str(r.id),
                netsuite_internal_id=r.netsuite_internal_id,
                amount=r.amount,
                currency=r.currency,
                transaction_date=r.transaction_date,
                memo=r.memo,
                related_payout_id=r.related_payout_id,
                subsidiary_id=r.subsidiary_id,
            )
            for r in rows
        ]

    async def _store_results(
        self,
        run_id: uuid.UUID,
        candidates: list[MatchCandidate],
    ) -> None:
        """Persist match candidates as ReconciliationResult rows."""
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

            # Use first deposit for the FK (or None for unmatched payouts)
            deposit_id = None
            if candidate.deposits:
                dep_id = candidate.deposits[0].id
                deposit_id = uuid.UUID(dep_id) if dep_id else None

            payout_uuid = None
            if candidate.payout.id:
                payout_uuid = uuid.UUID(candidate.payout.id)

            result = ReconciliationResult(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                run_id=run_id,
                payout_id=payout_uuid,
                deposit_id=deposit_id,
                match_type=candidate.match_type,
                confidence=candidate.confidence,
                status=status,
                stripe_amount=candidate.payout.net_amount if candidate.payout.id else None,
                netsuite_amount=candidate.deposits[0].amount if candidate.deposits else None,
                variance_amount=candidate.variance_amount,
                variance_type=candidate.variance_type,
                variance_explanation=candidate.variance_explanation,
                currency=candidate.payout.currency,
                match_rule=candidate.match_rule,
                evidence={
                    "payout_source_id": candidate.payout.source_id,
                    "deposit_ids": [d.netsuite_internal_id for d in candidate.deposits],
                    "signals": candidate.match_rule,
                },
            )
            self.db.add(result)

        await self.db.commit()
