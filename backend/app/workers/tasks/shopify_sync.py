from app.services.ingestion.shopify_sync import sync_shopify
from app.workers.base_task import InstrumentedTask, tenant_session
from app.workers.celery_app import celery_app


@celery_app.task(base=InstrumentedTask, bind=True, name="tasks.shopify_sync", queue="sync")
def shopify_sync(self, tenant_id: str, connection_id: str, **kwargs):
    """Sync Shopify data (orders, refunds, payments)."""
    with tenant_session(tenant_id) as db:
        result = sync_shopify(db, connection_id, tenant_id)
    return result
