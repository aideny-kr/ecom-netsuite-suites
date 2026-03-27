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
    from app.services.eval_case_miner import mine_organic_eval_cases, store_mined_cases
    from app.services.query_eval_harness import load_db_eval_cases, load_eval_cases
    from app.services.query_experiment_service import estimate_experiment_cost, run_single_experiment

    tenant_id = uuid.UUID(settings.QUERY_IMPROVEMENT_TENANT_ID)
    budget = settings.QUERY_IMPROVEMENT_BUDGET_USD
    max_experiments = settings.QUERY_IMPROVEMENT_MAX_EXPERIMENTS
    spent = 0.0
    stats = {
        "total": 0,
        "kept": 0,
        "reverted": 0,
        "skipped": 0,
        "errors": 0,
        "cost_usd": 0.0,
        "generated": 0,
        "mined": 0,
    }
    consecutive_errors = 0

    async with async_session_factory() as db:
        # Phase 0: Generate new synthetic eval cases from schema hints
        try:
            from app.services.eval_case_generator import generate_eval_cases

            for dialect in ["suiteql", "bigquery"]:
                generated = await generate_eval_cases(db, tenant_id, dialect, max_new=3)
                stats["generated"] += len(generated)
                if generated:
                    print(f"[AUTO_IMPROVE] Generated {len(generated)} new {dialect} eval cases", flush=True)
            await db.commit()
        except Exception as exc:
            print(f"[AUTO_IMPROVE] Generation failed (non-fatal): {exc}", flush=True)

        # Phase 1: Mine new organic eval cases from recent successful queries
        try:
            new_cases = await mine_organic_eval_cases(db, tenant_id)
            if new_cases:
                stored = await store_mined_cases(db, tenant_id, new_cases)
                await db.commit()
                stats["mined"] = stored
                print(f"[AUTO_IMPROVE] Mined {stored} new organic eval cases", flush=True)
        except Exception as exc:
            print(f"[AUTO_IMPROVE] Mining failed (non-fatal): {exc}", flush=True)

        # Phase 2: Load all eval cases (YAML seed + DB organic)
        suiteql_seed = load_eval_cases("suiteql")
        bigquery_seed = load_eval_cases("bigquery")
        suiteql_organic = await load_db_eval_cases(db, tenant_id, "suiteql")
        bigquery_organic = await load_db_eval_cases(db, tenant_id, "bigquery")

        # Combine: organic cases first (higher value — real user patterns), then seed
        suiteql_cases = suiteql_organic + suiteql_seed
        bigquery_cases = bigquery_organic + bigquery_seed

        # Interleave: alternate SuiteQL and BigQuery
        cases = []
        for i in range(max(len(suiteql_cases), len(bigquery_cases))):
            if i < len(suiteql_cases):
                cases.append(suiteql_cases[i])
            if i < len(bigquery_cases):
                cases.append(bigquery_cases[i])

        cases = cases[:max_experiments]
        print(
            f"[AUTO_IMPROVE] Starting: {len(cases)} experiments "
            f"({len(suiteql_organic)}+{len(bigquery_organic)} organic, "
            f"{len(suiteql_seed)}+{len(bigquery_seed)} seed), "
            f"budget ${budget}",
            flush=True,
        )

        # Per-dialect tracking for score history
        from collections import defaultdict

        _dialect_stats: dict[str, dict] = defaultdict(
            lambda: {
                "total": 0,
                "kept": 0,
                "reverted": 0,
                "skipped": 0,
                "errors": 0,
                "scores": [],
                "cost": 0.0,
            }
        )

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
                ds = _dialect_stats[case.dialect]
                ds["total"] += 1
                ds["cost"] += result.get("cost_usd", est_cost)
                exp_score = result.get("experiment_score", 0)
                if exp_score > 0:
                    ds["scores"].append(exp_score)

                if decision == "KEEP":
                    stats["kept"] += 1
                    ds["kept"] += 1
                elif decision == "REVERT":
                    stats["reverted"] += 1
                    ds["reverted"] += 1
                else:
                    stats["skipped"] += 1
                    ds["skipped"] += 1

                print(
                    f"[AUTO_IMPROVE] #{stats['total']} {case.dialect} "
                    f'q="{case.question[:50]}" → {decision} '
                    f"(score={result.get('experiment_score', 0):.2f})",
                    flush=True,
                )

            except Exception as exc:
                stats["errors"] += 1
                _dialect_stats[case.dialect]["errors"] += 1
                consecutive_errors += 1
                print(f"[AUTO_IMPROVE] Experiment error: {exc}", flush=True)
                if consecutive_errors >= 3:
                    print("[AUTO_IMPROVE] 3 consecutive errors, stopping", flush=True)
                    break

        # Write score history per dialect
        try:
            from datetime import date

            from app.models.eval_score_history import EvalScoreHistory

            for dial, dial_stats in _dialect_stats.items():
                if dial_stats["total"] == 0:
                    continue
                avg_score = (
                    round(sum(dial_stats["scores"]) / len(dial_stats["scores"]), 4) if dial_stats["scores"] else None
                )
                history = EvalScoreHistory(
                    tenant_id=tenant_id,
                    run_date=date.today(),
                    dialect=dial,
                    total_cases=dial_stats["total"],
                    kept=dial_stats["kept"],
                    reverted=dial_stats["reverted"],
                    skipped=dial_stats["skipped"],
                    errors=dial_stats["errors"],
                    avg_composite_score=avg_score,
                    cost_usd=dial_stats["cost"],
                )
                db.add(history)
        except Exception as exc:
            print(f"[AUTO_IMPROVE] Score history write failed (non-fatal): {exc}", flush=True)

        await db.commit()

    print(f"[AUTO_IMPROVE] Complete: {stats}", flush=True)
    return stats
