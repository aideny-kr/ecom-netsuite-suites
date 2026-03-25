"""Nightly autonomous query improvement task.

Runs experiments (up to 15 SuiteQL + 15 BigQuery), scores them,
and promotes winning patterns into the proven patterns store.
Budget: ~$10 ceiling, ~$2 typical per night.
Schedule: 5:00 AM UTC (avoids conflict with knowledge-crawler at 3:00 AM).
"""

import asyncio

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app


@celery_app.task(
    base=InstrumentedTask,
    bind=True,
    name="tasks.auto_query_improvement",
    queue="sync",
    soft_time_limit=1800,  # 30 min
    time_limit=2100,  # 35 min hard limit
)
def auto_query_improvement(self):
    """Run nightly query improvement experiments."""
    from app.core.config import settings

    if not settings.QUERY_IMPROVEMENT_ENABLED:
        print("[AUTO_IMPROVE] Disabled via QUERY_IMPROVEMENT_ENABLED=false", flush=True)
        return {"status": "disabled"}

    if not settings.QUERY_IMPROVEMENT_TENANT_ID:
        print("[AUTO_IMPROVE] No QUERY_IMPROVEMENT_TENANT_ID configured", flush=True)
        return {"status": "no_tenant"}

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run_experiments(settings))
    finally:
        loop.close()


async def _run_experiments(settings) -> dict:
    import uuid

    from app.core.database import async_session_factory
    from app.services.query_eval_harness import load_eval_cases
    from app.services.query_experiment_service import estimate_experiment_cost, run_single_experiment

    tenant_id = uuid.UUID(settings.QUERY_IMPROVEMENT_TENANT_ID)
    budget = settings.QUERY_IMPROVEMENT_BUDGET_USD
    max_experiments = settings.QUERY_IMPROVEMENT_MAX_EXPERIMENTS
    spent = 0.0
    stats = {"total": 0, "kept": 0, "reverted": 0, "skipped": 0, "errors": 0, "cost_usd": 0.0}
    consecutive_errors = 0

    # Load eval cases
    suiteql_cases = load_eval_cases("suiteql")
    bigquery_cases = load_eval_cases("bigquery")

    # Interleave: alternate SuiteQL and BigQuery
    cases = []
    for i in range(max(len(suiteql_cases), len(bigquery_cases))):
        if i < len(suiteql_cases):
            cases.append(suiteql_cases[i])
        if i < len(bigquery_cases):
            cases.append(bigquery_cases[i])

    cases = cases[:max_experiments]
    print(f"[AUTO_IMPROVE] Starting: {len(cases)} experiments, budget ${budget}", flush=True)

    async with async_session_factory() as db:
        for case in cases:
            # Budget check
            est_cost = estimate_experiment_cost(case.dialect)
            if spent + est_cost > budget:
                print(f"[AUTO_IMPROVE] Budget exhausted: ${spent:.2f}/{budget}", flush=True)
                break

            try:
                result = await run_single_experiment(
                    case=case,
                    tenant_id=tenant_id,
                    db=db,
                )
                spent += result.get("cost_usd", est_cost)
                stats["total"] += 1
                stats["cost_usd"] = round(spent, 4)
                consecutive_errors = 0

                decision = result.get("decision", "SKIP")
                if decision == "KEEP":
                    stats["kept"] += 1
                elif decision == "REVERT":
                    stats["reverted"] += 1
                else:
                    stats["skipped"] += 1

                print(
                    f"[AUTO_IMPROVE] #{stats['total']} {case.dialect} "
                    f'q="{case.question[:50]}" → {decision} '
                    f"(score={result.get('experiment_score', 0):.2f})",
                    flush=True,
                )

            except Exception as exc:
                stats["errors"] += 1
                consecutive_errors += 1
                print(f"[AUTO_IMPROVE] Experiment error: {exc}", flush=True)
                if consecutive_errors >= 3:
                    print("[AUTO_IMPROVE] 3 consecutive errors, stopping", flush=True)
                    break

        await db.commit()

    print(f"[AUTO_IMPROVE] Complete: {stats}", flush=True)
    return stats
