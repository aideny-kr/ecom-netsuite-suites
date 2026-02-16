import time

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app


@celery_app.task(base=InstrumentedTask, bind=True, name="tasks.example_sync", queue="sync")
def example_sync(self, tenant_id: str, connection_id: str | None = None, **kwargs):
    """
    No-op sync skeleton demonstrating InstrumentedTask instrumentation.
    In Phase 2+, this will be replaced with real sync logic.
    """
    time.sleep(1)  # Simulate work
    return {
        "records_fetched": 0,
        "records_created": 0,
        "records_updated": 0,
        "message": "Stub sync completed successfully",
    }
