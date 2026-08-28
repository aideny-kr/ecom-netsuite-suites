"""Celery task: nightly Celigo flow-map sync (Task 7 of the Plan B flow-map
plan) + its dispatch fan-out.

Modeled on `app/workers/tasks/suitescript_sync.py` (an async service wrapped
in its own event loop via `loop.run_until_complete` -- both connect to an
async-ORM service from a sync Celery task, unlike stripe_sync.py's sync
`tenant_session`) crossed with `app/workers/tasks/netsuite_deposit_sync_all
.py` / `stripe_sync_all.py`'s `DISPATCHABLE_CONNECTION_STATUSES` fan-out
(`celigo_flow_map_sync_all` below dispatches for a `status='error'`
connection too, per the 2026-07-29 incident's fix -- see that constant's own
docstring in `app/models/connection.py`).

⚠️ NOT ON THE BEAT SCHEDULE (human ruling, 2026-08-27 -- `.superpowers/sdd/
2026-08-25-celigo-plan-b-flow-map/progress.md`, "Pre-flight rulings"): a
nightly cron entry would fail on auth against the real tenant every night
until a working Celigo token exists. This task is fully built and tested;
wiring it to Beat is a ONE-LINE follow-up once a live token exists -- add to
`app/workers/celery_app.py`'s `conf.beat_schedule` dict:

    "celigo-flow-map-sync-nightly": {
        "task": "tasks.celigo_flow_map_sync_all",
        "schedule": crontab(hour=5, minute=0),  # pick a free slot
    },

`app.workers.tasks.celigo_flow_map_sync` IS added to `celery_app.py`'s
`conf.include` list (so Celery registers/imports it and a manual "Sync now"
call via `celery_app.send_task` works today) -- only `beat_schedule` is
deliberately left untouched.

FRESHNESS CURSOR lives on `cursor_states` (`object_type="celigo_flow_map"`,
via `app.services.ingestion.base.save_cursor_async`/`load_cursor`) -- NOT
`Connection.metadata_json`. `celigo_write_guard.py` refuses any ORM write to
a `provider='celigo'` `connections` row outside the paired connect/
disconnect endpoints, so a background sync task cannot legally touch that
row's metadata even for its own bookkeeping; `cursor_states` is an
unguarded, purpose-built table for exactly this. `save_cursor_async` is
called ONLY after `sync_flow_map_for_connection` returns without raising --
a raise anywhere in that multi-stage walk propagates out of `_execute`
uncaught, `InstrumentedTask.on_failure` records the job `failed`, and the
cursor row is left exactly where the LAST fully successful run left it. That
is what "advances only on success" means operationally.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import set_tenant_context, worker_async_session
from app.core.encryption import decrypt_credentials
from app.models.connection import Connection
from app.services.ingestion.base import save_cursor_async
from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


class CeligoSyncFailedError(RuntimeError):
    """Raised when `celigo_flow_map_sync` cannot run at all -- no matching,
    non-revoked Celigo connection for `(tenant_id, connection_id)`, a missing
    stored token, or `sync_flow_map_for_connection` itself raised (auth
    rejected, a malformed response, a DB constraint violation, etc.). Any
    raise here means `InstrumentedTask.on_failure` records the job
    `status='failed'` and -- the entire point of this task's design -- the
    `cursor_states` row is left untouched (see module docstring)."""


@celery_app.task(
    base=InstrumentedTask,
    bind=True,
    name="tasks.celigo_flow_map_sync",
    queue="sync",
    soft_time_limit=1800,
    time_limit=2100,
)
def celigo_flow_map_sync(self, tenant_id: str, connection_id: str, **kwargs):
    """Sync one tenant's Celigo flow map (integrations/flows/steps/scripts/
    errors) and record config drift. See module docstring for the
    freshness-cursor / Beat-schedule posture."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_execute(tenant_id, connection_id))
    finally:
        loop.close()


async def _execute(tenant_id: str, connection_id: str) -> dict:
    from dataclasses import asdict
    from datetime import datetime, timezone

    from app.services.celigo.sync_service import sync_flow_map_for_connection

    async with worker_async_session() as session:
        await set_tenant_context(session, tenant_id)

        # Scoped by id + tenant_id + provider, excluding only `revoked` --
        # mirrors connector_status.py's `_get_celigo_connection` (the one
        # place this codebase already defines "a usable Celigo connection
        # row"). Deliberately NOT filtered to ACTIVE_CONNECTION_STATUSES: a
        # `status='error'` connection must still be attempted here (the fix
        # for the 2026-07-29 incident lives in the FAN-OUT below choosing to
        # dispatch for it in the first place; this lookup must not undo that
        # by silently rejecting it a second time).
        result = await session.execute(
            select(Connection).where(
                Connection.id == uuid.UUID(connection_id),
                Connection.tenant_id == uuid.UUID(tenant_id),
                Connection.provider == "celigo",
                Connection.status != "revoked",
            )
        )
        connection = result.scalar_one_or_none()
        if connection is None:
            raise CeligoSyncFailedError("No Celigo connection found for this tenant")

        creds = decrypt_credentials(connection.encrypted_credentials)
        token = creds.get("token")
        if not token:
            raise CeligoSyncFailedError("Celigo connection has no stored token")
        region = (connection.metadata_json or {}).get("region", "us")

        # Any exception from here propagates uncaught -- see module docstring.
        summary = await sync_flow_map_for_connection(
            session,
            tenant_id=uuid.UUID(tenant_id),
            connection_id=connection.id,
            token=token,
            region=region,
        )

        # Freshness cursor -- ONLY reached if the call above did not raise.
        await save_cursor_async(
            session,
            connection.id,
            "celigo_flow_map",
            cursor_value=datetime.now(timezone.utc).isoformat(),
        )

        await session.commit()
        return asdict(summary)


# ---------------------------------------------------------------------------
# Fan-out -- dispatch across every DISPATCHABLE (not just active/healthy)
# Celigo connection. NOT registered on Beat (see module docstring).
# ---------------------------------------------------------------------------


def _find_dispatchable_celigo_connections(db: Session) -> list[dict]:
    """Mirrors `_find_active_stripe_connections`/`_find_active_netsuite_
    connections` EXACTLY -- `DISPATCHABLE_CONNECTION_STATUSES` (active/
    healthy/error), not `ACTIVE_CONNECTION_STATUSES`, so a connection that
    flipped to `error` still gets dispatched and produces a visible failed
    job instead of being silently skipped forever (2026-07-29 incident, same
    reliability class). `revoked` stays excluded -- there's no path back to
    health for it, and dispatching would just spam failures for a connection
    nobody intends to reactivate."""
    from app.models.connection import DISPATCHABLE_CONNECTION_STATUSES, Connection

    result = db.execute(
        select(Connection.id, Connection.tenant_id).where(
            Connection.provider == "celigo",
            Connection.status.in_(DISPATCHABLE_CONNECTION_STATUSES),
        )
    )
    return [{"connection_id": str(row[0]), "tenant_id": str(row[1])} for row in result.all()]


@celery_app.task(base=InstrumentedTask, name="tasks.celigo_flow_map_sync_all", queue="sync")
def celigo_flow_map_sync_all():
    """Iterate every dispatchable Celigo connection and dispatch per-tenant
    flow-map sync tasks. NOT on the Beat schedule -- see module docstring for
    the one-line follow-up once a live Celigo token exists."""
    from app.workers.base_task import sync_engine

    with Session(sync_engine) as db:
        connections = _find_dispatchable_celigo_connections(db)

    stats = {"dispatched": 0, "skipped": 0}
    for conn in connections:
        try:
            celery_app.send_task(
                "tasks.celigo_flow_map_sync",
                kwargs={"tenant_id": conn["tenant_id"], "connection_id": conn["connection_id"]},
                queue="sync",
            )
            stats["dispatched"] += 1
        except Exception:
            stats["skipped"] += 1
            logger.exception(
                "celigo_flow_map_sync_all.dispatch_failed",
                extra={"connection_id": conn["connection_id"]},
            )

    logger.info("celigo_flow_map_sync_all.completed", extra=stats)
    return stats
