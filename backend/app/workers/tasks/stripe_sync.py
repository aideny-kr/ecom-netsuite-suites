from sqlalchemy import select

from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
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

    DEFER: this task-level pre-flight guard and netsuite_deposit_sync's
    service-layer errors-list-then-raise are two different shapes for the
    same reliability-class fix (one is a query at the task boundary, the
    other threads an errors list through the service and checks it at the
    task boundary) -- not unified here; each sync's existing internal shape
    was kept rather than forcing both onto one pattern.
    """


@celery_app.task(base=InstrumentedTask, bind=True, name="tasks.stripe_sync", queue="sync")
def stripe_sync(self, tenant_id: str, connection_id: str, **kwargs):
    """Sync Stripe data (payouts, balance transactions, disputes)."""
    with tenant_session(tenant_id) as db:
        # Scoped by id + tenant_id + provider -- mirrors connection_service
        # .get_connection's (id, tenant_id) predicates (that helper is async
        # and this task uses a sync Session via tenant_session, so it can't be
        # reused directly; replicated here instead). tenant_id closes the gap
        # where a connection_id belonging to a DIFFERENT tenant would
        # otherwise still pass; provider closes the same class of gap for a
        # connection_id that happens to belong to a non-Stripe connection.
        # DEFER: this is a fresh fetch, not connection_service.get_connection
        # itself, so the guard and sync_stripe() below each independently
        # query the same row (double-fetch) -- tracked as a follow-up, not
        # fixed here.
        connection = db.execute(
            select(Connection).where(
                Connection.id == connection_id,
                Connection.tenant_id == tenant_id,
                Connection.provider == "stripe",
                Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
            )
        ).scalar_one_or_none()
        if connection is None:
            # DEFER (TOCTOU): this check only narrows the window, it doesn't
            # close it -- the connection could still flip status between this
            # SELECT and sync_stripe()'s own fetch a few lines below. Accepted
            # for now; the narrow window is a far smaller exposure than the
            # previous no-filter-at-all gap this guard replaces.
            raise StripeSyncFailedError("No active Stripe connection found")

        result = sync_stripe(db, connection_id, tenant_id)
    return result
