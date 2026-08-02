"""Celery task: sync NetSuite deposits via SuiteQL.

This is an async service that needs to run in an event loop,
so we use asyncio.run() inside the Celery task.
"""

import asyncio
import logging
from datetime import date, timedelta

from app.workers.base_task import InstrumentedTask
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Caps how many individual errors are joined into the raised exception message
# so a SuiteQL failure that repeats per-row doesn't produce an unbounded
# job.error_message; the remainder is summarized as "+N more".
_MAX_ERRORS_IN_MESSAGE = 5


def _format_errors(errors: list[str]) -> str:
    """Join ALL errors (not just the first) so the job row's error_message
    keeps the full picture -- capped at _MAX_ERRORS_IN_MESSAGE."""
    shown = errors[:_MAX_ERRORS_IN_MESSAGE]
    message = "; ".join(shown)
    remaining = len(errors) - len(shown)
    if remaining > 0:
        message += f" (+{remaining} more)"
    return message


class NetsuiteDepositSyncFailedError(RuntimeError):
    """Raised when the sync did NOTHING and the service reported at least one
    error (no active NetSuite connection, auth failure, SuiteQL failure) --
    a total failure.

    ``sync_netsuite_deposits`` never raises on this class of failure -- it
    appends to ``DepositSyncResult.errors`` and returns gracefully, because
    the recon pipeline's inline sync (a different caller) relies on that
    graceful return. This task-level check is what makes the difference
    visible for the SCHEDULED path: raising here (instead of returning the
    dict as-is) is what makes InstrumentedTask.on_failure record the job row
    `status='failed'` with the real reason, instead of on_success recording
    `status='completed'`.

    2026-07-29 incident: the Framework NetSuite connection silently flipped
    to `error` (OAuth token expired). The nightly Beat task kept recording
    `jobs.status='completed'` every night while syncing zero deposits --
    four days of mirror staleness were invisible in job history.
    """


@celery_app.task(base=InstrumentedTask, name="tasks.netsuite_deposit_sync", queue="sync")
def netsuite_deposit_sync(
    tenant_id: str,
    date_from: str | None = None,
    date_to: str | None = None,
    **kwargs,
):
    """Sync NetSuite deposits for a tenant.

    Uses async internals (netsuite_client, get_valid_token) so wraps in asyncio.run().
    Default: last 90 days if no dates provided.
    """
    from app.core.database import async_session_factory

    async def _run():
        from app.services.ingestion.netsuite_deposit_sync import sync_netsuite_deposits

        today = date.today()
        d_from = date.fromisoformat(date_from) if date_from else today - timedelta(days=90)
        d_to = date.fromisoformat(date_to) if date_to else today

        async with async_session_factory() as db:
            result = await sync_netsuite_deposits(
                db=db,
                tenant_id=tenant_id,
                date_from=d_from,
                date_to=d_to,
            )
            return {
                "records_synced": result.records_synced,
                "records_new": result.records_new,
                "records_updated": result.records_updated,
                "errors": result.errors,
            }

    summary = asyncio.run(_run())

    # Total failure: zero records synced AND at least one error reported.
    # Partial success (some records synced despite some row-level errors)
    # stays `completed`, carrying the errors in the returned summary (which
    # InstrumentedTask.on_success stores verbatim as `result_summary`).
    if summary["errors"] and summary["records_synced"] == 0:
        raise NetsuiteDepositSyncFailedError(_format_errors(summary["errors"]))

    return summary
