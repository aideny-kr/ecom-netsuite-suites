from sqlalchemy import select

from app.models.connection import Connection
from app.services.ingestion.stripe_sync import sync_stripe
from app.workers.base_task import InstrumentedTask, tenant_session
from app.workers.celery_app import celery_app


class StripeSyncFailedError(RuntimeError):
    """Raised when the Stripe sync task cannot run at all -- no active
    connection for the given ``connection_id``.

    Unlike ``sync_netsuite_deposits``, ``sync_stripe``'s own connection lookup
    has no status filter, so a connection that flipped to `error`/inactive
    would still be used with whatever stale credentials it has. This
    task-level guard runs BEFORE the service is called: no active connection
    is a total failure and must raise, so InstrumentedTask.on_failure records
    the job row `status='failed'` instead of a silently `completed` no-op --
    same reliability class as the NetSuite deposit-sync 2026-07-29 incident.

    The payout-status-refresh pass inside ``sync_stripe`` is deliberately
    fail-safe (its own per-row errors are swallowed and folded into
    `payouts_refresh_errors` in the returned summary) and must NEVER trip
    this guard -- only a missing/inactive connection does.
    """


@celery_app.task(base=InstrumentedTask, bind=True, name="tasks.stripe_sync", queue="sync")
def stripe_sync(self, tenant_id: str, connection_id: str, **kwargs):
    """Sync Stripe data (payouts, balance transactions, disputes)."""
    with tenant_session(tenant_id) as db:
        connection = db.execute(
            select(Connection).where(
                Connection.id == connection_id,
                Connection.status.in_(["active", "healthy"]),
            )
        ).scalar_one_or_none()
        if connection is None:
            raise StripeSyncFailedError("No active Stripe connection found")

        result = sync_stripe(db, connection_id, tenant_id)
    return result
