from app.services.ingestion.stripe_sync import sync_stripe
from app.workers.base_task import InstrumentedTask, tenant_session
from app.workers.celery_app import celery_app


@celery_app.task(base=InstrumentedTask, bind=True, name="tasks.stripe_sync", queue="sync")
def stripe_sync(self, tenant_id: str, connection_id: str, **kwargs):
    """Sync Stripe data (payouts, balance transactions, disputes)."""
    with tenant_session(tenant_id) as db:
        result = sync_stripe(db, connection_id, tenant_id)
    return result
