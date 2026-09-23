"""ResolutionAgent Celery task — Phase 2 of the summary-first recon rework.

Runs after planning (dispatched by the OrderReconJob hook and by the
plan-resolutions endpoint, both flag-gated: ``reconciliation`` AND
``recon_resolution_agent``, default OFF). Deterministically gathers context for
each planner abstention (``source='planner'``, ``action='needs_human'``,
``status='proposed'``), makes ONE forced-tool LLM classification call per item,
validates the output (allowlist, materiality guard, no-LLM-numbers contract),
and applies it as a supersede-then-insert (``source='agent'``) under the same
invariants as ``plan_run``. The agent NEVER writes to NetSuite; a failed or
timed-out item degrades to ``needs_human`` and the run continues.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.llm_adapter import get_adapter
from app.services.chat.nodes import get_tenant_ai_config
from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

MASTER_RECON_FLAG = "reconciliation"
AGENT_FLAG = "recon_resolution_agent"
PROGRESS_UPDATE_EVERY = 10


class ResolutionItemsNotPersistedError(RuntimeError):
    """Raised after every item was tried, when some proposals were not written, so the
    job reads ``failed`` rather than ``completed`` with the count buried in its summary."""


def _require_every_item_persisted(summary: dict) -> dict:
    failures = summary.get("persist_failures") or 0
    if failures:
        raise ResolutionItemsNotPersistedError(
            f"{failures} of {summary.get('processed')} items were not persisted; summary={summary}"
        )
    return summary


async def _recover_after_failed_write(db: AsyncSession, tenant_id: str) -> None:
    """Roll back, then re-apply the tenant context. The worker's context is a plain SET,
    which a rollback undoes when nothing has committed since it ran, so without this a
    failure on the first item would leave every later item with no tenant context. A
    failure to re-apply it is not swallowed: the run stops rather than go on unscoped.

    This restores the context on the session's current connection. A connection the
    pool replaces mid-run (recycle, failed pre-ping) starts without it; that gap is older
    than this helper and shared by every worker that uses set_tenant_context_session."""
    from app.core.database import set_tenant_context_session

    with contextlib.suppress(Exception):
        await db.rollback()
    await set_tenant_context_session(db, tenant_id)


def _update_job_progress(tenant_id: str, job_id, processed: int, total: int) -> None:
    """Best-effort progress update on the Job row via a short separate sync
    session — matches the session pattern InstrumentedTask itself uses."""
    from app.models.job import Job
    from app.workers.base_task import tenant_session

    try:
        with tenant_session(tenant_id) as session:
            job = session.get(Job, job_id)
            if job:
                job.result_summary = {"processed": processed, "total": total}
                session.commit()
    except Exception:
        logger.warning("resolution_agent.progress_update_failed", extra={"job_id": str(job_id)})


async def run_resolution_agent(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
    *,
    job_id: uuid.UUID | str | None = None,
) -> dict:
    """Core agent tail. Testable directly against a seeded DB session."""
    from sqlalchemy import select

    from app.models.reconciliation import ReconciliationRun
    from app.services import feature_flag_service
    from app.services.reconciliation.four_bucket_classifier import CLOSED_RUN_STATUSES
    from app.services.reconciliation.materiality import load_materiality
    from app.services.reconciliation.resolution_agent import (
        PER_ITEM_TIMEOUT_SECONDS,
        apply_agent_proposal,
        fetch_agent_eligible,
        gather_context,
    )
    from app.services.reconciliation.resolution_jev import decide_item
    from app.services.typesafe import client as jev_client
    from app.services.typesafe.audit import record_comparison

    tid = uuid.UUID(str(tenant_id))
    rid = uuid.UUID(str(run_id))

    if not await feature_flag_service.is_enabled(db, tid, MASTER_RECON_FLAG):
        return {"skipped": "flag_disabled"}
    if not await feature_flag_service.is_enabled(db, tid, AGENT_FLAG):
        return {"skipped": "flag_disabled"}

    # Close = hard freeze: a run can close between planning and the agent tail
    # picking it up (e.g. a manually-triggered re-plan on an old run). Never
    # investigate or write proposals for a closed/locked run.
    run = (
        await db.execute(
            select(ReconciliationRun).where(ReconciliationRun.id == rid, ReconciliationRun.tenant_id == tid)
        )
    ).scalar_one_or_none()
    if run is not None and run.status in CLOSED_RUN_STATUSES:
        return {"skipped": "run_closed"}

    items = await fetch_agent_eligible(db, tid, rid)
    total = len(items)
    if total == 0:
        return {
            "processed": 0,
            "upgraded": 0,
            "kept_needs_human": 0,
            "contract_violations": 0,
            "persist_failures": 0,
            "comparison_failures": 0,
        }

    provider, model, api_key, _is_byok = await get_tenant_ai_config(db, tid)
    adapter = get_adapter(provider, api_key)
    materiality = await load_materiality(db, tid)

    processed = 0
    upgraded = 0
    kept_needs_human = 0
    contract_violations = 0
    persist_failures = 0
    comparison_failures = 0

    from app.core.config import settings

    async with contextlib.AsyncExitStack() as stack:
        # One HTTPS connection for the whole run instead of a TLS handshake per item.
        # Entered only when Jev is actually on; if entering fails, items run without it.
        if settings.JEV_RECON_RESOLUTION_MODE in {"shadow", "live"} and settings.TYPESAFE_API_KEY:
            try:
                await stack.enter_async_context(jev_client.session())
            except Exception:
                logger.warning("resolution_agent.jev_session_unavailable", exc_info=True)

        # A rollback expires EVERY loaded instance (expire_on_commit=False does not cover
        # rollback), and touching an expired attribute on an async session is lazy IO that
        # raises MissingGreenlet — which would cascade one item's failure into every item
        # after it. So ids are snapshotted up front, and after any rollback each item is
        # reloaded with an awaited refresh before it is used.
        item_ids = [item.id for item in items]
        expired = False
        for item, item_id in zip(items, item_ids, strict=True):
            shadow = None
            if expired:
                try:
                    await db.refresh(item)
                except Exception:
                    logger.exception("resolution_agent.item_reload_failed", extra={"proposal_id": str(item_id)})
                    persist_failures += 1
                    processed += 1
                    # a database error here leaves the transaction aborted for every later item
                    await _recover_after_failed_write(db, str(tid))
                    continue
            try:
                context = await gather_context(db, tid, item)
                # decide_item is the LLM path unchanged when JEV_RECON_RESOLUTION_MODE
                # is off; both models' answers go through the same validate_output.
                validated, shadow = await asyncio.wait_for(
                    decide_item(tid, adapter, model, context, materiality),
                    timeout=PER_ITEM_TIMEOUT_SECONDS,
                )
            except Exception:
                logger.warning("resolution_agent.item_classification_failed", extra={"proposal_id": str(item_id)})
                validated = {
                    "action": "needs_human",
                    "narrative": "Agent classification failed; needs investigation.",
                    "key_evidence": [],
                    "contract_violation": "classification_error",
                }

            # Persistence is isolated per item: a commit that fails on one item must degrade
            # THAT item, never abort the run and strand the rest. The proposal and the
            # comparison are separate writes with separate outcomes — apply_agent_proposal
            # commits its own write, so a later failure recording the comparison must not
            # recount a durably applied item as a persist failure.
            persisted = True
            try:
                applied = await apply_agent_proposal(db, item, validated)
            except Exception:
                logger.exception("resolution_agent.item_persist_failed", extra={"proposal_id": str(item_id)})
                persist_failures += 1
                persisted = False
                applied = False
                await _recover_after_failed_write(db, str(tid))
                expired = True

            if shadow is not None:
                shadow["applied"] = bool(applied)  # False = nothing was written for this item
                try:
                    await record_comparison(
                        db,
                        tenant_id=tid,
                        category="reconciliation",
                        action="recon.jev_comparison",
                        payload=shadow,
                        resource_type="recon_resolution_proposal",
                        resource_id=str(item_id),
                        correlation_id=str(rid),
                    )
                    await db.commit()
                except Exception:
                    logger.exception(
                        "resolution_agent.jev_comparison_commit_failed", extra={"proposal_id": str(item_id)}
                    )
                    comparison_failures += 1
                    await _recover_after_failed_write(db, str(tid))
                    expired = True

            processed += 1
            if persisted:
                if validated.get("contract_violation"):
                    contract_violations += 1
                if validated["action"] == "needs_human":
                    kept_needs_human += 1
                else:
                    upgraded += 1

            if job_id and processed % PROGRESS_UPDATE_EVERY == 0:
                _update_job_progress(tenant_id, job_id, processed, total)

    if job_id:
        _update_job_progress(tenant_id, job_id, processed, total)

    return {
        "processed": processed,
        "upgraded": upgraded,
        "kept_needs_human": kept_needs_human,
        "contract_violations": contract_violations,
        "persist_failures": persist_failures,
        "comparison_failures": comparison_failures,
    }


def dispatch_resolution_agent(tenant_id: str, run_id: str) -> None:
    """Fire-and-forget enqueue. Failures to enqueue log a warning, never raise —
    the caller (plan hook / plan-resolutions endpoint) must never fail because
    the agent could not be scheduled."""
    try:
        celery_app.send_task(
            "tasks.recon_resolution_agent",
            kwargs={"tenant_id": str(tenant_id), "run_id": str(run_id)},
            queue="recon",
        )
    except Exception:
        logger.warning(
            "resolution_agent.dispatch_failed",
            extra={"tenant_id": str(tenant_id), "run_id": str(run_id)},
        )


@celery_app.task(base=InstrumentedTask, name="tasks.recon_resolution_agent", queue="recon", bind=True)
def recon_resolution_agent(self, tenant_id: str, run_id: str, **kwargs) -> dict:
    """Per-run agent tail. Opens its own RLS-scoped session.

    Session-scoped SET (not SET LOCAL): apply_agent_proposal commits once per
    item it processes, which would clear a transaction-scoped GUC after the
    FIRST item, silently dropping RLS context for every item after it. Safe
    here because worker_async_session() is a disposable per-task engine, never
    a pooled session returned to a shared pool (see database.py docstring)."""
    from app.core.database import set_tenant_context_session, worker_async_session

    async def _run() -> dict:
        async with worker_async_session() as db:
            await set_tenant_context_session(db, tenant_id)
            summary = await run_resolution_agent(db, tenant_id, run_id, job_id=self._job_id)
        # Raised outside the session: worker_async_session disposes its engine only on a
        # normal exit, so an exception unwinding through it would skip the cleanup.
        return _require_every_item_persisted(summary)

    return asyncio.run(_run())
