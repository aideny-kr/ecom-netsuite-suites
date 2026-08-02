"""Scheduled Celery task: sync all active Stripe connections across tenants.

Runs hourly via Beat. Dispatches per-tenant stripe_sync tasks.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _find_active_stripe_connections(db: Session) -> list[dict]:
    """Find all DISPATCHABLE Stripe connections across all tenants.

    Deliberately includes `error`-status connections (not just active/healthy)
    -- see DISPATCHABLE_CONNECTION_STATUSES. Same reliability class as the
    2026-07-29 NetSuite incident: a connection that flipped to `error` used to
    be filtered out HERE, before dispatch, so it was silently skipped every
    hour forever and the child task's active-connection guard never got a
    chance to run. Dispatching for it lets that guard (workers/tasks/
    stripe_sync.py, active-only) reject the connection and the task raise,
    producing a visible failed job row every cycle the connection stays dead.
    `revoked`/other intentionally-dead statuses stay excluded.
    """
    from app.models.connection import DISPATCHABLE_CONNECTION_STATUSES, Connection

    result = db.execute(
        select(Connection.id, Connection.tenant_id).where(
            Connection.provider == "stripe",
            Connection.status.in_(DISPATCHABLE_CONNECTION_STATUSES),
        )
    )
    return [{"connection_id": str(row[0]), "tenant_id": str(row[1])} for row in result.all()]


@celery_app.task(base=InstrumentedTask, name="tasks.stripe_sync_all", queue="sync")
def stripe_sync_all():
    """Iterate all active Stripe connections and dispatch per-tenant sync tasks."""
    from app.workers.base_task import sync_engine

    with Session(sync_engine) as db:
        connections = _find_active_stripe_connections(db)

    stats = {"dispatched": 0, "skipped": 0}

    for conn in connections:
        try:
            celery_app.send_task(
                "tasks.stripe_sync",
                kwargs={
                    "tenant_id": conn["tenant_id"],
                    "connection_id": conn["connection_id"],
                },
                queue="sync",
            )
            stats["dispatched"] += 1
        except Exception:
            stats["skipped"] += 1
            logger.exception(
                "stripe_sync_all.dispatch_failed",
                extra={"connection_id": conn["connection_id"]},
            )

    logger.info("stripe_sync_all.completed", extra=stats)
    return stats
