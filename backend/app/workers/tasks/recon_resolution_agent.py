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
    from app.services import feature_flag_service
    from app.services.reconciliation.materiality import load_materiality
    from app.services.reconciliation.resolution_agent import (
        PER_ITEM_TIMEOUT_SECONDS,
        apply_agent_proposal,
        classify_item,
        fetch_agent_eligible,
        gather_context,
        validate_output,
    )

    tid = uuid.UUID(str(tenant_id))
    rid = uuid.UUID(str(run_id))

    if not await feature_flag_service.is_enabled(db, tid, MASTER_RECON_FLAG):
        return {"skipped": "flag_disabled"}
    if not await feature_flag_service.is_enabled(db, tid, AGENT_FLAG):
        return {"skipped": "flag_disabled"}

    items = await fetch_agent_eligible(db, tid, rid)
    total = len(items)
    if total == 0:
        return {"processed": 0, "upgraded": 0, "kept_needs_human": 0, "contract_violations": 0}

    provider, model, api_key, _is_byok = await get_tenant_ai_config(db, tid)
    adapter = get_adapter(provider, api_key)
    materiality = await load_materiality(db, tid)

    processed = 0
    upgraded = 0
    kept_needs_human = 0
    contract_violations = 0

    for item in items:
        try:
            context = await gather_context(db, tid, item)
            out = await asyncio.wait_for(
                classify_item(adapter, model, context),
                timeout=PER_ITEM_TIMEOUT_SECONDS,
            )
            validated = validate_output(out, context, materiality)
        except Exception:
            logger.warning("resolution_agent.item_classification_failed", extra={"proposal_id": str(item.id)})
            validated = {
                "action": "needs_human",
                "narrative": "Agent classification failed; needs investigation.",
                "key_evidence": [],
                "contract_violation": "classification_error",
            }

        if validated.get("contract_violation"):
            contract_violations += 1
        if validated["action"] == "needs_human":
            kept_needs_human += 1
        else:
            upgraded += 1

        await apply_agent_proposal(db, item, validated)
        processed += 1

        if job_id and processed % PROGRESS_UPDATE_EVERY == 0:
            _update_job_progress(tenant_id, job_id, processed, total)

    if job_id:
        _update_job_progress(tenant_id, job_id, processed, total)

    return {
        "processed": processed,
        "upgraded": upgraded,
        "kept_needs_human": kept_needs_human,
        "contract_violations": contract_violations,
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
    """Per-run agent tail. Opens its own RLS-scoped session."""
    from app.core.database import set_tenant_context, worker_async_session

    async def _run() -> dict:
        async with worker_async_session() as db:
            await set_tenant_context(db, tenant_id)
            return await run_resolution_agent(db, tenant_id, run_id, job_id=self._job_id)

    return asyncio.run(_run())
