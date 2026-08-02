from app.services.ingestion.shopify_sync import sync_shopify
from app.workers.base_task import InstrumentedTask, tenant_session
from app.workers.celery_app import celery_app


@celery_app.task(base=InstrumentedTask, bind=True, name="tasks.shopify_sync", queue="sync")
def shopify_sync(self, tenant_id: str, connection_id: str, **kwargs):
    """Sync Shopify data (orders, refunds, payments).

    DEFER: same reliability-class gap as stripe_sync had pre-fix -- no
    active-connection guard here at all, so a connection that flipped to
    `error`/inactive would still be used with stale credentials, and
    sync_shopify's own connection lookup has no tenant_id scoping either.
    Out of scope for this pass (fix/sync-job-honesty); tracked as a separate
    ticket, same shape as the netsuite/stripe fixes in this module's siblings.
    """
    with tenant_session(tenant_id) as db:
        result = sync_shopify(db, connection_id, tenant_id)
    return result
