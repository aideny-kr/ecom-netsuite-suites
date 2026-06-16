from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "ecom_netsuite",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

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
)

celery_app.conf.include = [
    "app.workers.tasks.agent_benchmark_vs_mcp",
    "app.workers.tasks.audit_retention",
    "app.workers.tasks.auto_learning",
    "app.workers.tasks.auto_query_improvement",
    "app.workers.tasks.billing_sync",
    "app.workers.tasks.connection_health",
    "app.workers.tasks.example_sync",
    "app.workers.tasks.knowledge_crawler",
    "app.workers.tasks.metadata_discovery",
    "app.workers.tasks.metric_catalog_reseed",
    "app.workers.tasks.onboarding_discovery",
    "app.workers.tasks.oracle_skill_reseed",
    "app.workers.tasks.proactive_token_refresh",
    "app.workers.tasks.shopify_sync",
    "app.workers.tasks.stripe_health_check",
    "app.workers.tasks.stripe_sync",
    "app.workers.tasks.stripe_sync_all",
    "app.workers.tasks.netsuite_deposit_sync",
    "app.workers.tasks.netsuite_deposit_sync_all",
    "app.workers.tasks.reconciliation_run",
    "app.workers.tasks.recon_scheduled_run_all",
    "app.workers.tasks.recon_envelope_dry_run",
    "app.workers.tasks.suitescript_sync",
    "app.workers.tasks.suiteql_export",
    "app.workers.tasks.workspace_run",
    "app.workers.tasks.drive_rag_sync",
    "app.workers.tasks.tenant_memory_extract_backfill",
]

celery_app.conf.beat_schedule = {
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
}
