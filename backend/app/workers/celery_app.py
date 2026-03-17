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
    "app.workers.tasks.billing_sync",
    "app.workers.tasks.connection_health",
    "app.workers.tasks.example_sync",
    "app.workers.tasks.knowledge_crawler",
    "app.workers.tasks.metadata_discovery",
    "app.workers.tasks.shopify_sync",
    "app.workers.tasks.stripe_sync",
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
}
