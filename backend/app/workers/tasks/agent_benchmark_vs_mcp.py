"""Nightly agent benchmark vs Claude + Oracle NetSuite MCP.

Runs the vs-MCP benchmark suite against the configured tenant, persists
results to `agent_benchmark_runs`, then compares today's average
accuracy delta (ours - mcp) against yesterday's to surface regressions.

A regression is defined as:
    today's mean delta_accuracy < yesterday's mean delta_accuracy - REGRESSION_EPSILON

When a regression is detected, the task emits:
  1. A structured log at ERROR level (shows up in GCP Cloud Logging)
  2. A Sentry capture_message() with tag `regression=agent_vs_mcp`
  3. stderr output so beat/worker logs also surface it

Run manually to smoke-test:
    docker exec ecom-netsuite-backend-1 python -c "
    from app.workers.tasks.agent_benchmark_vs_mcp import run_nightly_benchmark_sync
    print(run_nightly_benchmark_sync())
    "
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import date, timedelta

import structlog

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

logger = structlog.get_logger()


# Fraction of accuracy drop that counts as a regression. 0.1 = "we lost
# 10% absolute accuracy on average vs yesterday." Tight enough to catch
# silent drift, loose enough to avoid alert fatigue from variation.
REGRESSION_EPSILON = 0.10


@celery_app.task(
    base=InstrumentedTask,
    bind=True,
    name="tasks.agent_benchmark_vs_mcp",
    queue="sync",
    soft_time_limit=1800,  # 30 min
    time_limit=2100,  # 35 min hard
)
def agent_benchmark_vs_mcp_task(self):
    """Nightly: run vs-MCP benchmark and check for regressions."""
    if not _is_enabled():
        print("[AGENT_BENCHMARK] Disabled via AGENT_BENCHMARK_VS_MCP_ENABLED=false", flush=True)
        return {"status": "disabled"}

    tenant_id_str = os.environ.get("AGENT_BENCHMARK_TENANT_ID")
    if not tenant_id_str:
        print("[AGENT_BENCHMARK] No AGENT_BENCHMARK_TENANT_ID configured, skipping", flush=True)
        return {"status": "no_tenant"}

    suite = os.environ.get("AGENT_BENCHMARK_SUITE", "sales")
    agent_model = os.environ.get("AGENT_BENCHMARK_AGENT_MODEL", "claude-sonnet-4-6")
    baseline_model = os.environ.get("AGENT_BENCHMARK_BASELINE_MODEL", "claude-sonnet-4-6")

    try:
        tenant_id = uuid.UUID(tenant_id_str)
    except ValueError:
        print(f"[AGENT_BENCHMARK] Invalid AGENT_BENCHMARK_TENANT_ID: {tenant_id_str}", flush=True)
        return {"status": "invalid_tenant"}

    return asyncio.run(
        _run_nightly_benchmark(
            tenant_id=tenant_id,
            suite=suite,
            agent_model=agent_model,
            baseline_model=baseline_model,
        )
    )


def _is_enabled() -> bool:
    raw = os.environ.get("AGENT_BENCHMARK_VS_MCP_ENABLED", "false")
    return raw.strip().lower() in ("1", "true", "yes", "on")


async def _run_nightly_benchmark(
    *,
    tenant_id: uuid.UUID,
    suite: str,
    agent_model: str,
    baseline_model: str,
    emitter=None,
) -> dict:
    """Run the benchmark and compare to yesterday's run."""
    from app.core.database import async_session_factory, set_tenant_context
    from app.services.benchmarks.agent_runner import run_agent
    from app.services.benchmarks.baseline_runner import run_baseline
    from app.services.benchmarks.persistence import persist_case_result
    from app.services.benchmarks.run_vs_mcp import (
        _run_single_case,
        load_cases,
    )

    # Silence unused-import warnings — these are used indirectly via the
    # run_vs_mcp module but we import them here so a nightly run doesn't
    # die with a late import error inside a worker.
    _ = (run_agent, run_baseline)

    run_id = uuid.uuid4()
    run_date = date.today()

    try:
        cases = load_cases(suite=suite)
    except Exception as exc:
        print(f"[AGENT_BENCHMARK] load_cases failed: {exc}", flush=True)
        return {"status": "load_error", "error": str(exc)}

    print(
        f"[AGENT_BENCHMARK] run_id={run_id} tenant={tenant_id} "
        f"suite={suite} cases={len(cases)} agent={agent_model} baseline={baseline_model}",
        flush=True,
    )

    if emitter:
        emitter.emit("run_started", {
            "total_cases": len(cases),
            "estimated_cost_usd": len(cases) * 0.35,  # rough benchmark estimate
        })

    stats = {
        "run_id": str(run_id),
        "run_date": run_date.isoformat(),
        "cases_total": len(cases),
        "cases_run": 0,
        "cases_persisted": 0,
        "ours_wins": 0,
        "mcp_wins": 0,
        "ties": 0,
        "failures": 0,
        "avg_delta_accuracy": 0.0,
    }

    deltas: list[float] = []

    async with async_session_factory() as db:
        await set_tenant_context(db, str(tenant_id))

        for i, case in enumerate(cases, 1):
            if emitter and emitter.cancelled():
                print("[AGENT_BENCHMARK] Cancelled via emitter", flush=True)
                break
            if emitter:
                emitter.emit("case_started", {
                    "case_id": case.case_id,
                    "question": case.question,
                    "index": i,
                })
            print(
                f"[AGENT_BENCHMARK] [{i}/{len(cases)}] {case.case_id}",
                flush=True,
            )
            try:
                result = await _run_single_case(
                    case=case,
                    tenant_id=tenant_id,
                    agent_model=agent_model,
                    baseline_model=baseline_model,
                    skip_baseline=False,
                    use_llm_judge=True,
                    db=db,
                )
            except Exception as exc:
                print(f"[AGENT_BENCHMARK] case crashed: {exc}", flush=True)
                stats["failures"] += 1
                continue

            stats["cases_run"] += 1

            if result.verdict == "OURS WINS":
                stats["ours_wins"] += 1
            elif result.verdict == "MCP WINS":
                stats["mcp_wins"] += 1
            elif result.verdict == "TIE":
                stats["ties"] += 1
            else:
                stats["failures"] += 1

            if result.mcp is not None:
                deltas.append(result.ours.answer_acc - result.mcp.answer_acc)

            if emitter:
                emitter.emit("case_complete", {
                    "case_id": case.case_id,
                    "result": {
                        "verdict": result.verdict,
                        "ours_accuracy": result.ours.answer_acc if result.ours else 0.0,
                        "mcp_accuracy": result.mcp.answer_acc if result.mcp else 0.0,
                    },
                    "running_cost_usd": 0.0,  # reconciled by Celery wrapper from agent_benchmark_runs
                    "cases_completed": stats["cases_run"],
                })

            # Persist both sides
            try:
                if result.ours_raw is not None:
                    await persist_case_result(
                        db=db,
                        tenant_id=tenant_id,
                        run_id=run_id,
                        run_date=run_date,
                        case_id=case.case_id,
                        side="ours",
                        model=agent_model,
                        result=result.ours_raw,
                        answer_accuracy=result.ours.answer_acc,
                        tool_accuracy=result.ours.tool_acc,
                    )
                if result.mcp_raw is not None:
                    await persist_case_result(
                        db=db,
                        tenant_id=tenant_id,
                        run_id=run_id,
                        run_date=run_date,
                        case_id=case.case_id,
                        side="mcp",
                        model=baseline_model,
                        result=result.mcp_raw,
                        answer_accuracy=result.mcp.answer_acc if result.mcp else 0.0,
                        tool_accuracy=result.mcp.tool_acc if result.mcp else 0.0,
                    )
                await db.commit()
                stats["cases_persisted"] += 1
            except Exception as exc:
                print(f"[AGENT_BENCHMARK] persist failed for {case.case_id}: {exc}", flush=True)
                await db.rollback()

        # Compute avg delta for today
        today_avg_delta = sum(deltas) / len(deltas) if deltas else 0.0
        stats["avg_delta_accuracy"] = round(today_avg_delta, 4)

        # Compare to yesterday
        yesterday = run_date - timedelta(days=1)
        yesterday_delta = await _get_avg_delta_for_date(
            db=db,
            tenant_id=tenant_id,
            target_date=yesterday,
        )

    print(
        f"[AGENT_BENCHMARK] DONE: wins={stats['ours_wins']} losses={stats['mcp_wins']} "
        f"ties={stats['ties']} failures={stats['failures']} "
        f"avg_delta_acc={today_avg_delta:+.3f} (yesterday: {yesterday_delta:+.3f})",
        flush=True,
    )

    # Regression check
    if yesterday_delta is not None:
        drop = yesterday_delta - today_avg_delta
        if drop > REGRESSION_EPSILON:
            _emit_regression_alert(
                tenant_id=tenant_id,
                today_avg_delta=today_avg_delta,
                yesterday_avg_delta=yesterday_delta,
                drop=drop,
                stats=stats,
            )
            stats["regression_detected"] = True
        else:
            stats["regression_detected"] = False
    else:
        stats["regression_detected"] = False

    stats["yesterday_delta"] = (
        round(yesterday_delta, 4) if yesterday_delta is not None else None
    )

    if emitter:
        emitter.emit("run_complete", {
            "status": "completed",
            "summary": stats,
            "total_cost_usd": 0.0,  # reconciled by Celery wrapper from agent_benchmark_runs
        })

    # Send email digest (daily summary + regression alert)
    try:
        from app.services.benchmark_email_service import send_benchmark_digest

        send_benchmark_digest(
            run_date=run_date,
            stats=stats,
            regression_detected=stats.get("regression_detected", False),
        )
    except Exception as exc:
        print(f"[AGENT_BENCHMARK] email digest failed (non-fatal): {exc}", flush=True)

    return stats


async def _get_avg_delta_for_date(
    *,
    db,
    tenant_id: uuid.UUID,
    target_date: date,
) -> float | None:
    """Fetch the average (ours - mcp) accuracy delta for a given date.

    Returns None if there's no run on that date.
    """
    from sqlalchemy import text as sa_text

    result = await db.execute(
        sa_text("""
            WITH daily AS (
                SELECT case_id,
                       AVG(CASE WHEN side = 'ours' THEN answer_accuracy END) AS ours_acc,
                       AVG(CASE WHEN side = 'mcp' THEN answer_accuracy END) AS mcp_acc
                FROM agent_benchmark_runs
                WHERE tenant_id = :t AND run_date = :d
                GROUP BY case_id
            )
            SELECT AVG(ours_acc - mcp_acc) FROM daily
            WHERE ours_acc IS NOT NULL AND mcp_acc IS NOT NULL
        """),
        {"t": str(tenant_id), "d": target_date.isoformat()},
    )
    row = result.first()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def _emit_regression_alert(
    *,
    tenant_id: uuid.UUID,
    today_avg_delta: float,
    yesterday_avg_delta: float,
    drop: float,
    stats: dict,
) -> None:
    """Loud-fail a regression: structured log + Sentry + stderr."""
    msg = (
        f"AGENT VS MCP REGRESSION: accuracy delta dropped by {drop:+.3f} "
        f"vs yesterday (today={today_avg_delta:+.3f}, yesterday={yesterday_avg_delta:+.3f})"
    )

    # Structured log at ERROR — surfaces in GCP Cloud Logging + log-based alerts
    logger.error(
        "agent_benchmark.regression_detected",
        tenant_id=str(tenant_id),
        today_avg_delta=round(today_avg_delta, 4),
        yesterday_avg_delta=round(yesterday_avg_delta, 4),
        drop=round(drop, 4),
        stats=stats,
    )

    # Sentry capture (best-effort)
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            scope.set_tag("regression", "agent_vs_mcp")
            scope.set_tag("tenant_id", str(tenant_id))
            scope.set_extra("stats", stats)
            sentry_sdk.capture_message(msg, level="error")
    except Exception:
        # Sentry is best-effort — never crash the task over it
        pass

    # stderr so beat/worker docker logs also surface it loudly
    print(f"[AGENT_BENCHMARK] !!! {msg}", file=sys.stderr, flush=True)


def run_nightly_benchmark_sync() -> dict:
    """Entry point for one-off manual testing from a python shell.

    Not bound to Celery so you can run it from `docker exec`.
    """
    tenant_id_str = os.environ.get("AGENT_BENCHMARK_TENANT_ID", "ce3dfaad-626f-4992-84e9-500c8291ca0a")
    tenant_id = uuid.UUID(tenant_id_str)
    suite = os.environ.get("AGENT_BENCHMARK_SUITE", "sales")
    return asyncio.run(
        _run_nightly_benchmark(
            tenant_id=tenant_id,
            suite=suite,
            agent_model="claude-sonnet-4-6",
            baseline_model="claude-sonnet-4-6",
        )
    )
