"""Celery wrapper around the existing benchmark/experiment tasks.

The wrapper guarantees agent_lab_runs.status never stays 'running' after
the worker finishes — try/except/finally with finalize_run_sync in the
finally block.

Dispatched via apply_async from service.start_run().
"""

from __future__ import annotations

import asyncio
import uuid

from app.core.redis_client import get_sync_redis
from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app


@celery_app.task(
    base=InstrumentedTask,
    bind=True,
    name="tasks.agent_lab_run",
    queue="sync",
    soft_time_limit=1800,  # 30 min
    time_limit=2100,  # 35 min hard
)
def agent_lab_run_task(self, run_id: str, config: dict):
    """Run a benchmark or experiment on behalf of the agent-lab UI.

    Args:
        run_id: UUID of the AgentLabRun row (as string — Celery serializes)
        config: {kind: "benchmark" | "experiment",
                 mode: "all" | "single",
                 case_id: optional,
                 tenant_id: stringified UUID}
    """
    from app.core.database import get_sync_session
    from app.services.agent_lab import service
    from app.services.agent_lab.progress_emitter import ProgressEmitter

    run_uuid = uuid.UUID(run_id)
    tenant_uuid = uuid.UUID(config["tenant_id"])

    status = "failed"
    cost = 0.0
    error: str | None = None

    r = get_sync_redis()
    with get_sync_session() as db:
        emitter = ProgressEmitter(run_uuid, r, db)
        try:
            if config["kind"] == "benchmark":
                from app.workers.tasks.agent_benchmark_vs_mcp import _run_nightly_benchmark
                asyncio.run(_run_nightly_benchmark(
                    tenant_id=tenant_uuid,
                    suite="sales",
                    agent_model="claude-sonnet-4-6",
                    baseline_model="claude-sonnet-4-6",
                    emitter=emitter,
                ))
                # Benchmark return dict has no total_cost — reconcile from persisted rows
                cost = service.sum_benchmark_cost_for_run(db, run_uuid)
            else:  # experiment
                from app.core.config import settings as app_settings
                from app.workers.tasks.auto_query_improvement import _run_experiments
                stats = asyncio.run(_run_experiments(app_settings, emitter=emitter))
                cost = float(stats.get("cost_usd", 0.0))

            status = "cancelled" if emitter.cancelled() else "completed"
        except Exception as exc:
            error = str(exc)[:500]  # cap for column width
            # status stays "failed"
            print(f"[AGENT_LAB_RUN] run_id={run_id} failed: {exc}", flush=True)
        finally:
            # Emit terminal event so SSE subscribers get a close signal
            try:
                emitter.emit("run_complete", {
                    "status": status,
                    "total_cost_usd": cost,
                    "error_message": error,
                })
            except Exception:
                pass  # Redis down shouldn't block finalize

            # Persist final status — guaranteed regardless of success/failure/cancel
            service.finalize_run_sync(
                db=db, run_id=run_uuid,
                status=status, cost_usd_actual=cost,
                error_message=error,
            )

    return {"run_id": run_id, "status": status, "cost_usd": cost}
