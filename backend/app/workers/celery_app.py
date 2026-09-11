from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "ecom_netsuite",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)


def _default_send_task_priority(name, args, kwargs, options, task=None, **kw):
    """Catch-all `task_routes` entry — MUST stay LAST in the tuple below.
    `celery.app.routes.Router.lookup_route` tries each router in list order
    and returns the first non-None result (verified against the installed
    `celery/app/routes.py`), so the explicit dict ahead of this one always
    wins for the two names it names, and this callable only ever fires for
    everything else.

    Round 4 (fix/jobs-live-run-defects): round 3's fix set
    `task_default_priority = 6` believing it capped unlabeled batch work
    (e.g. `tasks.transaction_ops_run`) below scheduled-jobs' own elevated
    sends. It never did: `task_default_priority` only becomes `Task.priority`
    — a class attribute `celery/app/task.py`'s `from_config` table binds,
    read ONLY by a BOUND task's own `.apply_async()`/`.delay()`.
    `celery_app.send_task(name, ...)` — the "send by name" API
    `app.services.transaction_ops.scheduler.publish_investigation` (and
    `report_auto_refresh.py`, `solidus_sync.py`, `recon_scheduled_run_all.py`,
    every other `send_task` caller in this codebase) actually uses — never
    touches a `Task` object at all, so it never read that default. Without an
    explicit `priority=` kwarg of its own, such a call published at whatever
    priority an unset message defaults to on the Redis transport (priority 0
    — the FIRST bucket kombu's transport polls, same as scheduled-jobs' own
    elevated "Run now"/sweep sends), silently defeating the whole point of a
    lower number for them.

    A `task_routes` entry, by contrast, IS consulted by both `send_task` and
    `Task.apply_async` (`Router.route()` is the one call every dispatch path
    shares — see `test_celery_config.py`), so this is the correct mechanism.
    `queue` is left untouched (`None` here means "don't route the queue" —
    `Router.expand_destination` only touches `queue` when present in the
    returned dict), so this never overrides a caller's own `queue=` kwarg."""
    return {"priority": 6}


celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    beat_schedule_filename="/data/celerybeat-schedule",
    task_default_queue="default",
    task_queues={
        "default": {"exchange": "default", "routing_key": "default"},
        "sync": {"exchange": "sync", "routing_key": "sync"},
        "recon": {"exchange": "recon", "routing_key": "recon"},
        "export": {"exchange": "export", "routing_key": "export"},
    },
    # Redis transport supports per-message priority with no new queues and no
    # worker flags. On staging the single shared worker was flooded by
    # another feature's batch task (hundreds of messages on `sync`/`recon`),
    # starving a human's "Run now" and the Beat sweep behind the flood — see
    # `app.workers.tasks.scheduled_jobs`'s SCHEDULED_JOBS_*_PRIORITY
    # constants for who actually publishes at an elevated priority.
    #
    # We rely entirely on kombu's OWN defaults here rather than overriding
    # `broker_transport_options`: kombu's Redis transport polls priority
    # buckets in ASCENDING order (`Transport.Channel._brpop_start` builds its
    # BRPOP key list `for pri in priority_steps for queue in queues`, and
    # `priority_steps` defaults to `[0, 3, 6, 9]`) — so priority 0 is served
    # FIRST, 9 LAST. Do NOT set `queue_order_strategy` (it replaces
    # round-robin fairness across default/sync/recon/export with a fixed
    # order — an unrelated regression) or `sep` (changes the Redis key names
    # a worker addresses — old and new workers would talk past each other
    # during a rolling deploy).
    #
    # Round 4: `task_routes` (not `task_default_priority`, see
    # `_default_send_task_priority`'s docstring) is the actual broker-priority
    # mechanism. The two literal task names below MUST stay in sync with
    # `app.workers.tasks.scheduled_jobs.SCHEDULED_JOBS_RUN_NOW_PRIORITY`/
    # `SCHEDULED_JOBS_SWEEP_PRIORITY` (not imported here — that module already
    # imports `celery_app` from this one, so importing back would be
    # circular); `tests/test_celery_config.py` pins the two in sync.
    # `_default_send_task_priority` (module-level function above, LAST in
    # this tuple) is the catch-all for every other task name, including
    # unlabeled batch work like `tasks.transaction_ops_run`.
    task_routes=(
        {
            "tasks.scheduled_jobs_run_now": {"queue": "sync", "priority": 0},
            "tasks.scheduled_jobs_sweep": {"queue": "sync", "priority": 3},
        },
        _default_send_task_priority,
    ),
    # Kept ONLY as the `Task.priority` fallback for a bound task's own
    # `.apply_async()`/`.delay()` call (several call sites in this codebase
    # do that directly: `app/main.py`, several `mcp/tools/*`,
    # `api/v1/workspaces.py`, `drive_rag_sync.py`, `onboarding_service.py`).
    # It is NOT what protects scheduled-jobs' priority any more — see
    # `_default_send_task_priority`'s docstring above.
    task_default_priority=6,
)

celery_app.conf.include = [
    "app.workers.tasks.agent_benchmark_vs_mcp",
    "app.workers.tasks.audit_retention",
    "app.workers.tasks.auto_learning",
    "app.workers.tasks.auto_query_improvement",
    "app.workers.tasks.billing_sync",
    "app.workers.tasks.celigo_flow_map_sync",
    "app.workers.tasks.transaction_ops",
    "app.workers.tasks.connection_health",
    "app.workers.tasks.example_sync",
    "app.workers.tasks.knowledge_crawler",
    "app.workers.tasks.metadata_discovery",
    "app.workers.tasks.metric_catalog_reseed",
    "app.workers.tasks.onboarding_discovery",
    "app.workers.tasks.oracle_skill_reseed",
    "app.workers.tasks.proactive_token_refresh",
    "app.workers.tasks.shopify_sync",
    "app.workers.tasks.solidus_sync",
    "app.workers.tasks.stripe_health_check",
    "app.workers.tasks.stripe_sync",
    "app.workers.tasks.stripe_sync_all",
    "app.workers.tasks.netsuite_deposit_sync",
    "app.workers.tasks.netsuite_deposit_sync_all",
    "app.workers.tasks.reconciliation_run",
    "app.workers.tasks.recon_scheduled_run_all",
    "app.workers.tasks.recon_envelope_dry_run",
    "app.workers.tasks.recon_resolution_agent",
    "app.workers.tasks.report_auto_refresh",
    "app.workers.tasks.rolling_period_compose",
    "app.workers.tasks.scheduled_jobs",
    "app.workers.tasks.suitescript_sync",
    "app.workers.tasks.suiteql_export",
    "app.workers.tasks.workspace_run",
    "app.workers.tasks.drive_rag_sync",
    "app.workers.tasks.tenant_memory_extract_backfill",
]

celery_app.conf.beat_schedule = {
    "transaction-operations-actions-minute": {
        "task": "tasks.transaction_ops_collect_actions",
        "schedule": 60.0,
    },
    "transaction-operations-minute": {
        "task": "tasks.transaction_ops_collect_due",
        "schedule": 60.0,
    },
    "sync-metered-billing": {
        "task": "tasks.billing_sync",
        "schedule": 3600.0,  # hourly
    },
    "check-connection-health": {
        "task": "tasks.connection_health",
        "schedule": 900.0,  # every 15 minutes
    },
    "knowledge-crawler": {
        "task": "tasks.knowledge_crawler",
        "schedule": crontab(hour=3, minute=0),
    },
    "auto-learning": {
        "task": "tasks.auto_learning",
        "schedule": crontab(hour=4, minute=0),
    },
    "auto-query-improvement": {
        "task": "tasks.auto_query_improvement",
        "schedule": crontab(hour=10, minute=0),
    },
    # vs-MCP agent benchmark — runs nightly and alerts on regression.
    # Gated by AGENT_BENCHMARK_VS_MCP_ENABLED env var (default false).
    # Runs at 11:00 UTC, AFTER auto-query-improvement (10:00) so the
    # benchmark measures the state AFTER the nightly pattern promotion.
    "agent-benchmark-vs-mcp": {
        "task": "tasks.agent_benchmark_vs_mcp",
        "schedule": crontab(hour=11, minute=0),
    },
    "proactive-token-refresh": {
        "task": "tasks.proactive_token_refresh",
        "schedule": 300.0,  # every 5 minutes
    },
    "stripe-health-check": {
        "task": "tasks.stripe_health_check",
        "schedule": 900.0,  # every 15 minutes
    },
    "stripe-sync-nightly": {
        "task": "tasks.stripe_sync_all",
        "schedule": crontab(hour=1, minute=0),  # 1 AM UTC nightly
    },
    "netsuite-deposit-sync-nightly": {
        "task": "tasks.netsuite_deposit_sync_all",
        "schedule": crontab(hour=2, minute=0),  # 2 AM UTC nightly, 7-day delta
    },
    # Bet 3 Rung 1 — both flag-gated per tenant (default off → no-op fan-outs).
    # 03:30 UTC: after deposit sync (02:00) has landed the night's data.
    "recon-scheduled-run-nightly": {
        "task": "tasks.recon_scheduled_run_all",
        "schedule": crontab(hour=3, minute=30),
    },
    # 04:30 UTC: after scheduled runs complete; report-only envelope evaluation.
    "recon-envelope-dry-run-nightly": {
        "task": "tasks.recon_envelope_dry_run_all",
        "schedule": crontab(hour=4, minute=30),
    },
    "drive-rag-sync-nightly": {
        "task": "tasks.drive_rag_sync_all",
        "schedule": crontab(hour=6, minute=0),  # 06:00 UTC nightly
    },
    # Live-dashboard reports (Slice C): the hourly tick drives BOTH hourly and daily
    # reports — daily-ness is enforced per-report by the sweep's due computation, so
    # daily reports refresh at whatever hour they come due (spreads tenant NetSuite
    # load instead of a nightly stampede). :10 avoids the top-of-hour crunch of the
    # interval-scheduled tasks. Gated by REPORT_AUTO_REFRESH_ENABLED (default false).
    "report-auto-refresh-hourly": {
        "task": "tasks.report_auto_refresh_all",
        "schedule": crontab(minute=10),
    },
    "rolling-period-compose": {
        # Daily. A NetSuite period closes once a month, so a daily sweep is ~30x more
        # often than strictly needed and still leaves the wall at most one day stale.
        # 03:20 keeps it clear of the :10 hourly report refresh.
        "task": "tasks.rolling_period_compose_all",
        "schedule": crontab(hour=3, minute=20),
    },
    "oracle-skill-reseed": {
        "task": "tasks.oracle_skill_reseed",
        "schedule": 6 * 60 * 60,  # every 6 hours; re-seeds when skills-lock.json hashes change
    },
    # Keeps the SYSTEM metric catalog populated on fresh/staging DBs (no empty
    # catalog on deploy). Idempotent DELETE-then-INSERT; daily is enough for
    # static system metrics.
    "metric-catalog-reseed": {
        "task": "tasks.metric_catalog_reseed",
        "schedule": crontab(hour=5, minute=30),  # 05:30 UTC daily
    },
    # Scheduled Jobs platform (Slice 2, spec §B4). Every minute — a schedule's
    # own cron cadence (e.g. weekly) is what actually gates a run; this just
    # needs to be frequent enough that "Monday 06:00" fires close to 06:00.
    # Gated by SCHEDULED_JOBS_ENABLED (default true — see config.py's comment
    # on why default-on is safe here: every schedule additionally needs a
    # human-approved plan before run_due_jobs will touch it).
    "scheduled-jobs-sweep": {
        "task": "tasks.scheduled_jobs_sweep_all",
        "schedule": 60.0,
    },
}
