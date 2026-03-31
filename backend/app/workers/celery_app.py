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
    "app.workers.tasks.audit_retention",
    "app.workers.tasks.auto_learning",
    "app.workers.tasks.auto_query_improvement",
    "app.workers.tasks.billing_sync",
    "app.workers.tasks.connection_health",
    "app.workers.tasks.example_sync",
    "app.workers.tasks.knowledge_crawler",
    "app.workers.tasks.metadata_discovery",
    "app.workers.tasks.onboarding_discovery",
    "app.workers.tasks.proactive_token_refresh",
    "app.workers.tasks.shopify_sync",
    "app.workers.tasks.stripe_health_check",
    "app.workers.tasks.stripe_sync",
    "app.workers.tasks.stripe_sync_all",
    "app.workers.tasks.netsuite_deposit_sync",
    "app.workers.tasks.netsuite_deposit_sync_all",
    "app.workers.tasks.suitescript_sync",
    "app.workers.tasks.suiteql_export",
    "app.workers.tasks.workspace_run",
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
}
