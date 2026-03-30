"""Reconciliation pipeline orchestrator: pre-flight sync → match → classify → complete.

Wraps ReconJobRunner with SSE progress events and pre-flight data sync.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import NetsuitePosting, Payout, PayoutLine
from app.models.connection import Connection
from app.services.ingestion.netsuite_deposit_sync import sync_netsuite_deposits
from app.services.reconciliation.recon_job import ReconJobRunner

logger = structlog.get_logger()

# Pipeline stages in order
STAGES = [
    {"id": "preflight", "label": "Validating connections", "weight": 5},
    {"id": "sync_stripe", "label": "Syncing Stripe payouts", "weight": 20},
    {"id": "sync_netsuite", "label": "Syncing NetSuite deposits", "weight": 25},
    {"id": "matching", "label": "Running matching engine", "weight": 35},
    {"id": "classifying", "label": "Classifying results", "weight": 10},
    {"id": "complete", "label": "Finalizing", "weight": 5},
]


class ReconPipeline:
    """Orchestrates reconciliation with pre-flight sync and progress events."""

    def __init__(
        self,
        db: AsyncSession,
        tenant_id: str,
        queue: asyncio.Queue | None = None,
    ) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.queue = queue
        self._current_stage_idx = 0

    async def _emit(self, event: dict[str, Any]) -> None:
        """Push a progress event to the SSE queue."""
        if self.queue:
            await self.queue.put(event)

    async def _emit_stage(
        self,
        stage_id: str,
        status: str = "running",
        message: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Emit a stage progress event."""
        stage_idx = next((i for i, s in enumerate(STAGES) if s["id"] == stage_id), 0)
        # Calculate cumulative progress
        progress = sum(s["weight"] for s in STAGES[:stage_idx])
        if status == "completed":
            progress += STAGES[stage_idx]["weight"]

        event = {
            "type": "recon_progress",
            "stage": stage_id,
            "stage_label": STAGES[stage_idx]["label"],
            "status": status,
            "progress": min(progress, 100),
            "message": message,
        }
        if detail:
            event["detail"] = detail
        await self._emit(event)

    async def run(
        self,
        date_from: date,
        date_to: date,
        subsidiary_id: str | None = None,
        payout_ids: list[str] | None = None,
        match_level: str = "order",
    ) -> dict[str, Any]:
        """Execute full reconciliation pipeline with progress events.

        Returns the final summary dict.
        """
        run_id = None
        try:
            # ── Stage 1: Pre-flight checks ──────────────────────────────
            await self._emit_stage("preflight", "running", "Checking data source connections...")
            preflight = await self._preflight_check()
            await self._emit_stage(
                "preflight",
                "completed",
                f"Stripe: {'connected' if preflight['stripe_ok'] else 'not configured'} · "
                f"NetSuite: {'connected' if preflight['netsuite_ok'] else 'not configured'}",
                detail=preflight,
            )

            if not preflight["stripe_ok"] and not preflight["netsuite_ok"]:
                await self._emit(
                    {
                        "type": "recon_error",
                        "error": (
                            "No data sources configured. Connect Stripe or configure "
                            "NetSuite deposits in Settings → Data Source Connectors."
                        ),
                    }
                )
                return {"error": "No data sources configured"}

            # ── Stage 2: Sync Stripe payouts ────────────────────────────
            stripe_count = 0
            if preflight["stripe_ok"]:
                # Smart skip: if data was synced within the last hour, skip re-sync
                stripe_fresh = await self._is_stripe_fresh(preflight["stripe_connection_id"])
                existing_payouts = await self._count_payouts(date_from, date_to, subsidiary_id, payout_ids)

                if stripe_fresh and existing_payouts > 0:
                    await self._emit_stage(
                        "sync_stripe",
                        "completed",
                        f"Data is fresh — using {existing_payouts} existing payouts",
                    )
                else:
                    await self._emit_stage("sync_stripe", "running", "Pulling latest Stripe payouts...")
                    try:
                        stripe_count = await asyncio.wait_for(
                            self._sync_stripe(preflight["stripe_connection_id"]),
                            timeout=90.0,
                        )
                        await self._emit_stage(
                            "sync_stripe",
                            "completed",
                            f"Synced {stripe_count} payouts from Stripe",
                        )
                    except asyncio.TimeoutError:
                        if existing_payouts > 0:
                            await self._emit_stage(
                                "sync_stripe",
                                "completed",
                                f"Sync timed out — using {existing_payouts} existing payouts",
                            )
                        else:
                            await self._emit_stage(
                                "sync_stripe",
                                "error",
                                "Sync timed out. Click Sync Now in Settings first.",
                            )
            else:
                await self._emit_stage("sync_stripe", "skipped", "Stripe not configured — using existing payout data")

            # ── Stage 3: Sync NetSuite deposits ─────────────────────────
            ns_count = 0
            if preflight["netsuite_ok"]:
                # Smart skip: if deposits exist for this date range, skip re-sync
                existing_deposits = await self._count_deposits(date_from, date_to, subsidiary_id)
                ns_fresh = await self._is_netsuite_fresh(preflight["netsuite_connection_id"])

                if ns_fresh and existing_deposits > 0:
                    await self._emit_stage(
                        "sync_netsuite",
                        "completed",
                        f"Data is fresh — using {existing_deposits} existing deposits",
                    )
                    ns_count = existing_deposits
                else:
                    await self._emit_stage("sync_netsuite", "running", "Pulling NetSuite bank deposits via SuiteQL...")
                    ns_result = await sync_netsuite_deposits(
                        db=self.db,
                        tenant_id=self.tenant_id,
                        date_from=date_from,
                        date_to=date_to,
                    )
                    ns_count = ns_result.records_synced
                    if ns_result.errors:
                        await self._emit_stage(
                            "sync_netsuite",
                            "completed",
                            f"Synced {ns_count} deposits (warnings: {'; '.join(ns_result.errors[:2])})",
                        )
                    else:
                        await self._emit_stage(
                            "sync_netsuite",
                            "completed",
                            f"Synced {ns_count} deposits from NetSuite",
                        )
            else:
                await self._emit_stage(
                    "sync_netsuite",
                    "skipped",
                    "NetSuite REST not configured — using existing deposit data",
                )

            # Check we have data to reconcile
            deposit_count = await self._count_deposits(date_from, date_to, subsidiary_id)

            if match_level == "order":
                source_count = await self._count_charges(date_from, date_to, subsidiary_id)
                source_label = "charges"
            else:
                source_count = await self._count_payouts(date_from, date_to, subsidiary_id, payout_ids)
                source_label = "payouts"

            if source_count == 0 and deposit_count == 0:
                await self._emit(
                    {
                        "type": "recon_error",
                        "error": (
                            f"No {source_label} or deposits found for {date_from} – {date_to}. "
                            "Sync your data sources first."
                        ),
                    }
                )
                return {"error": "No data found for date range"}

            # ── Stage 4: Run matching engine ────────────────────────────
            await self._emit_stage(
                "matching",
                "running",
                f"Matching {source_count} {source_label} vs {deposit_count} deposits...",
            )

            if match_level == "order":
                from app.services.reconciliation.order_recon_job import OrderReconJob

                runner = OrderReconJob(db=self.db, tenant_id=self.tenant_id)
                summary = await runner.run(
                    date_from=date_from,
                    date_to=date_to,
                    subsidiary_id=subsidiary_id,
                )
            else:
                runner = ReconJobRunner(db=self.db, tenant_id=self.tenant_id)
                summary = await runner.run(
                    date_from=date_from,
                    date_to=date_to,
                    subsidiary_id=subsidiary_id,
                    payout_ids=payout_ids,
                )
            run_id = summary.run_id

            await self._emit_stage(
                "matching",
                "completed",
                f"Matched {summary.matched_count} of {summary.total_payouts} {source_label}",
            )

            # ── Stage 5: Classify results ───────────────────────────────
            await self._emit_stage("classifying", "running", "Classifying exceptions and variances...")
            # Classification happens inside the matching engine already
            await self._emit_stage(
                "classifying",
                "completed",
                f"{summary.exception_count} exceptions · {summary.unmatched_count} unmatched",
            )

            # ── Stage 6: Complete ───────────────────────────────────────
            match_rate = (
                Decimal(summary.matched_count) / Decimal(summary.total_payouts) * 100
                if summary.total_payouts > 0
                else Decimal("0")
            )

            await self._emit_stage(
                "complete",
                "completed",
                f"Reconciliation complete — {match_rate.quantize(Decimal('0.1'))}% match rate",
            )

            result = {
                "type": "recon_complete",
                "run_id": summary.run_id,
                "status": summary.status,
                "total_payouts": summary.total_payouts,
                "total_deposits": summary.total_deposits,
                "matched_count": summary.matched_count,
                "exception_count": summary.exception_count,
                "unmatched_count": summary.unmatched_count,
                "total_variance": str(summary.total_variance),
                "match_rate": str(summary.match_rate),
            }
            await self._emit(result)
            return result

        except Exception as e:
            logger.exception("recon_pipeline.failed", tenant_id=self.tenant_id, error=str(e))
            await self._emit(
                {
                    "type": "recon_error",
                    "error": f"Reconciliation failed: {str(e)}",
                    "run_id": run_id,
                }
            )
            raise

    async def _preflight_check(self) -> dict[str, Any]:
        """Check which data sources are available for this tenant."""
        # Check Stripe connection
        stripe_result = await self.db.execute(
            select(Connection.id).where(
                Connection.tenant_id == self.tenant_id,
                Connection.provider == "stripe",
                Connection.status.in_(["active", "healthy"]),
            )
        )
        stripe_conn = stripe_result.scalar_one_or_none()

        # Check NetSuite REST connection
        ns_result = await self.db.execute(
            select(Connection.id).where(
                Connection.tenant_id == self.tenant_id,
                Connection.provider == "netsuite",
                Connection.status.in_(["active", "healthy"]),
            )
        )
        ns_conn = ns_result.scalar_one_or_none()

        return {
            "stripe_ok": stripe_conn is not None,
            "stripe_connection_id": str(stripe_conn) if stripe_conn else None,
            "netsuite_ok": ns_conn is not None,
            "netsuite_connection_id": str(ns_conn) if ns_conn else None,
        }

    async def _sync_stripe(self, connection_id: str) -> int:
        """Run Stripe sync in a thread with sub-progress events."""
        from app.services.ingestion.stripe_sync import sync_stripe
        from app.workers.base_task import tenant_session

        loop = asyncio.get_running_loop()

        def _progress_callback(count: int, stage: str = "payouts"):
            """Thread-safe callback — schedules SSE event on the async loop."""
            loop.call_soon_threadsafe(
                self.queue.put_nowait if self.queue else (lambda _: None),
                {
                    "type": "recon_progress",
                    "stage": "sync_stripe",
                    "status": "running",
                    "message": f"Synced {count} payouts...",
                    "progress": 5 + min(count / 10, 15),  # 5-20% range
                },
            )

        def _run_sync():
            with tenant_session(self.tenant_id) as db:
                result = sync_stripe(
                    db=db,
                    connection_id=connection_id,
                    tenant_id=self.tenant_id,
                    progress_callback=_progress_callback,
                )
                return result.get("payouts_synced", 0)

        return await asyncio.to_thread(_run_sync)

    async def _count_payouts(
        self,
        date_from: date,
        date_to: date,
        subsidiary_id: str | None,
        payout_ids: list[str] | None,
    ) -> int:
        """Count payouts available for matching."""
        stmt = (
            select(func.count())
            .select_from(Payout)
            .where(
                Payout.tenant_id == self.tenant_id,
                Payout.status == "paid",
            )
        )
        if payout_ids:
            stmt = stmt.where(Payout.source_id.in_(payout_ids))
        else:
            stmt = stmt.where(Payout.arrival_date >= date_from, Payout.arrival_date <= date_to)
        if subsidiary_id:
            stmt = stmt.where(Payout.subsidiary_id == subsidiary_id)
        result = await self.db.execute(stmt)
        return result.scalar() or 0

    async def _count_charges(
        self,
        date_from: date,
        date_to: date,
        subsidiary_id: str | None,
    ) -> int:
        """Count payout_lines (charges) available for order-level matching."""
        from datetime import timedelta

        buffer = timedelta(days=5)
        stmt = (
            select(func.count())
            .select_from(PayoutLine)
            .join(Payout, PayoutLine.payout_id == Payout.id)
            .where(
                PayoutLine.tenant_id == self.tenant_id,
                PayoutLine.line_type == "charge",
                Payout.arrival_date >= date_from - buffer,
                Payout.arrival_date <= date_to + buffer,
            )
        )
        if subsidiary_id:
            stmt = stmt.where(PayoutLine.subsidiary_id == subsidiary_id)
        result = await self.db.execute(stmt)
        return result.scalar() or 0

    async def _count_deposits(
        self,
        date_from: date,
        date_to: date,
        subsidiary_id: str | None,
    ) -> int:
        """Count deposits available for matching."""
        stmt = (
            select(func.count())
            .select_from(NetsuitePosting)
            .where(
                NetsuitePosting.tenant_id == self.tenant_id,
                NetsuitePosting.record_type.in_(["deposit", "custdep", "bankdeposit", "journalentry"]),
                NetsuitePosting.transaction_date >= date_from,
                NetsuitePosting.transaction_date <= date_to,
            )
        )
        if subsidiary_id:
            stmt = stmt.where(NetsuitePosting.subsidiary_id == subsidiary_id)
        result = await self.db.execute(stmt)
        return result.scalar() or 0

    async def _is_stripe_fresh(self, connection_id: str) -> bool:
        """Check if Stripe data was synced within the last hour."""
        from datetime import datetime, timezone

        from app.models.pipeline import CursorState

        result = await self.db.execute(
            select(CursorState.last_synced_at).where(
                CursorState.connection_id == connection_id,
                CursorState.object_type == "stripe_payouts",
            )
        )
        last_sync = result.scalar_one_or_none()
        if not last_sync:
            return False
        elapsed = (datetime.now(timezone.utc) - last_sync).total_seconds()
        return elapsed < 86400  # 24 hours — data syncs hourly via Beat

    async def _is_netsuite_fresh(self, connection_id: str) -> bool:
        """Check if NetSuite deposit data was synced within the last hour."""
        from datetime import datetime, timezone

        from app.models.pipeline import CursorState

        result = await self.db.execute(
            select(CursorState.last_synced_at).where(
                CursorState.connection_id == connection_id,
                CursorState.object_type == "netsuite_deposits",
            )
        )
        last_sync = result.scalar_one_or_none()
        if not last_sync:
            return False
        elapsed = (datetime.now(timezone.utc) - last_sync).total_seconds()
        return elapsed < 86400  # 24 hours — data syncs hourly via Beat
