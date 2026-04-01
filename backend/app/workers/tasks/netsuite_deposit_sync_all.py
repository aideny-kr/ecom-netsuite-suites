"""Scheduled Celery task: sync NetSuite deposits for all tenants with active connections.

Runs nightly via Beat. Uses incremental sync (last 7 days) instead of full 90-day window.
"""

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _find_active_netsuite_connections(db: Session) -> list[dict]:
    """Find all active NetSuite REST connections across all tenants."""
    from app.models.connection import Connection

    result = db.execute(
        select(Connection.id, Connection.tenant_id).where(
            Connection.provider == "netsuite",
            Connection.status.in_(["active", "healthy"]),
        )
    )
    return [{"connection_id": str(row[0]), "tenant_id": str(row[1])} for row in result.all()]


@celery_app.task(base=InstrumentedTask, name="tasks.netsuite_deposit_sync_all", queue="sync")
def netsuite_deposit_sync_all():
    """Iterate all active NetSuite connections and dispatch per-tenant deposit sync tasks.

    Uses a 7-day incremental window (delta sync) instead of the full 90-day window.
    """
    from app.workers.base_task import sync_engine

    with Session(sync_engine) as db:
        connections = _find_active_netsuite_connections(db)

    today = date.today()
    date_from = (today - timedelta(days=7)).isoformat()
    date_to = today.isoformat()

    stats = {"dispatched": 0, "skipped": 0}

    for conn in connections:
        try:
            celery_app.send_task(
                "tasks.netsuite_deposit_sync",
                kwargs={
                    "tenant_id": conn["tenant_id"],
                    "date_from": date_from,
                    "date_to": date_to,
                },
                queue="sync",
            )
            stats["dispatched"] += 1
        except Exception:
            stats["skipped"] += 1
            logger.exception(
                "netsuite_deposit_sync_all.dispatch_failed",
                extra={"tenant_id": conn["tenant_id"]},
            )

    logger.info("netsuite_deposit_sync_all.completed", extra=stats)
    return stats
