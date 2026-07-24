"""Stripe ingestion: sync payouts, balance transactions (payout lines), and disputes."""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import stripe
import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.encryption import decrypt_credentials
from app.models.canonical import Dispute, Payout, PayoutLine
from app.models.connection import Connection
from app.services.ingestion.base import load_cursor, save_cursor, upsert_canonical

logger = structlog.get_logger()

# Stripe payout statuses that will never change again once reached. Anything else
# (`pending`, `in_transit`) is a candidate for the status-refresh pass below.
_NON_TERMINAL_PAYOUT_STATUSES = ("pending", "in_transit")

# Caps the per-cycle backlog drain (today's real backlog is ~38) so a large
# backlog can't blow the Supabase statement timeout; drains oldest-first across
# cycles rather than trying to do it all in one pass.
_STATUS_REFRESH_BATCH_LIMIT = 200


def _stripe_epoch_to_date(epoch: int | None) -> date | None:
    """Convert a Stripe Unix-epoch timestamp to a UTC calendar date.

    ``date.fromtimestamp()`` interprets the epoch in the HOST's local timezone,
    which can shift the date by one day for epochs near midnight UTC depending on
    where the process runs. Stripe timestamps are UTC, so always go through the
    epoch's UTC wall-clock date, never the host's local one.
    """
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).date()


def _count_active_stripe_connections(db: Session, tenant_id: str) -> int:
    """Count active Stripe connections for a tenant.

    Mirrors the active-status semantics of ``stripe_sync_all._find_active_stripe_connections``
    (status in active/healthy).
    """
    return db.execute(
        select(func.count())
        .select_from(Connection)
        .where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "stripe",
            Connection.status.in_(["active", "healthy"]),
        )
    ).scalar_one()


def refresh_payout_statuses(db: Session, connection_id: str, tenant_id: str) -> dict:
    """Re-fetch non-terminal payouts from Stripe and update stale status/arrival_date.

    The incremental sync above is cursored on Stripe's `created` timestamp, which
    never changes when a payout transitions status (e.g. in_transit -> paid) — so
    once synced, a non-terminal payout is never revisited by that loop and its
    status/arrival_date silently go stale (verified live 2026-07-24: 10/10 sampled
    non-terminal payouts were `paid` at Stripe but still `in_transit` in the
    mirror). This pass re-fetches each `pending`/`in_transit` payout by source_id
    and updates the canonical row when Stripe disagrees.

    Idempotent (re-running with no real change is a no-op) and defensive: a
    per-row error (Stripe fetch, timestamp parse, or comparison/assignment) is
    logged and skipped, never aborts the pass or the surrounding sync.

    ``Payout`` has no ``connection_id`` column (the Stripe API key is
    per-connection), so this can't scope the refresh query to one connection —
    only to the tenant. A tenant with more than one active Stripe connection
    would have this pass poll every connection's payouts using whichever
    connection's sync cycle happened to trigger it, silently erroring against
    the wrong key. Until there's a real per-row connection link, only run when
    the tenant has exactly one active Stripe connection.
    """
    active_connections = _count_active_stripe_connections(db, tenant_id)
    if active_connections != 1:
        logger.warning(
            "stripe_sync.status_refresh.skipped_multi_connection",
            connection_id=connection_id,
            tenant_id=tenant_id,
            active_stripe_connections=active_connections,
        )
        return {"checked": 0, "updated": 0, "errors": 0, "skipped": True}

    # Re-establish RLS tenant context: by the time this pass runs, the payouts
    # sync loop above has already committed several times, and SET LOCAL is
    # scoped to whatever transaction was live when tenant_session() first set
    # it — already long gone.
    from app.workers.base_task import set_tenant_context_sync

    set_tenant_context_sync(db, tenant_id)

    rows = (
        db.execute(
            select(Payout)
            .where(
                Payout.tenant_id == tenant_id,
                Payout.source == "stripe",
                Payout.status.in_(_NON_TERMINAL_PAYOUT_STATUSES),
            )
            .order_by(Payout.arrival_date.asc())
            .limit(_STATUS_REFRESH_BATCH_LIMIT)
        )
        .scalars()
        .all()
    )

    checked = 0
    updated = 0
    errors = 0
    processed = 0

    for row in rows:
        checked += 1
        try:
            stripe_payout = stripe.Payout.retrieve(row.source_id)
            # LOAD-BEARING ORDER: both new values are fully computed into locals
            # BEFORE either ORM assignment below. The per-row except relies on
            # this — any failure (fetch/parse) happens pre-assignment, so a row
            # can never be left half-mutated for the finally-block commit to
            # persist. Do not interleave per-field parse+assign.
            new_status = stripe_payout.status
            new_arrival_date = _stripe_epoch_to_date(stripe_payout.arrival_date)

            if new_status != row.status or new_arrival_date != row.arrival_date:
                row.status = new_status
                row.arrival_date = new_arrival_date
                updated += 1
        except Exception:
            errors += 1
            logger.warning(
                "stripe_sync.status_refresh.fetch_failed",
                connection_id=connection_id,
                payout_source_id=row.source_id,
                exc_info=True,
            )
        finally:
            # Batch commit every 10 rows (Supabase 2min statement timeout). This
            # runs regardless of a row error above — it used to sit after the
            # try/except and a failed row's `continue` would skip straight past
            # a boundary commit, deferring it to the next one.
            processed += 1
            if processed % 10 == 0:
                db.commit()
                set_tenant_context_sync(db, tenant_id)

    db.commit()
    # The trailing commit also clears SET LOCAL — re-establish so the phases
    # that run after this function (payout_lines, disputes) never execute with
    # a dead tenant context (standing rule: set-local-tenant-context-mid-commit).
    set_tenant_context_sync(db, tenant_id)

    summary = {"checked": checked, "updated": updated, "errors": errors}
    logger.info("stripe_sync.status_refresh", connection_id=connection_id, **summary)
    return summary


def sync_stripe(
    db: Session,
    connection_id: str,
    tenant_id: str,
    progress_callback=None,
) -> dict:
    """Run a full incremental sync for a Stripe connection.

    Args:
        progress_callback: Optional callable(payouts_synced, stage) for progress reporting.
                          Called every ~20 payouts during sync.

    Returns a summary dict with counts of records synced.
    """
    # ---- bootstrap --------------------------------------------------------
    connection = db.execute(select(Connection).where(Connection.id == connection_id)).scalar_one()

    creds = decrypt_credentials(connection.encrypted_credentials)
    stripe.api_key = creds["api_key"]

    payouts_synced = 0
    payout_lines_synced = 0
    disputes_synced = 0

    # ---- payouts ----------------------------------------------------------
    logger.info("stripe_sync.payouts.start", connection_id=connection_id)

    cursor = load_cursor(db, connection_id, "stripe_payouts")
    list_params: dict = {"limit": 100}
    if cursor:
        list_params["created"] = {"gt": int(cursor)}

    last_created = None
    synced_payout_stripe_ids: list[str] = []

    for payout in stripe.Payout.list(**list_params).auto_paging_iter():
        upsert_canonical(
            db,
            model_class=Payout,
            tenant_id=tenant_id,
            dedupe_key=f"stripe:{payout.id}",
            data={
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "dedupe_key": f"stripe:{payout.id}",
                "source": "stripe",
                "source_id": payout.id,
                "amount": Decimal(str(payout.amount / 100)),
                "fee_amount": Decimal("0"),
                "net_amount": Decimal(str(payout.amount / 100)),
                "currency": payout.currency.upper(),
                "status": payout.status,
                "arrival_date": _stripe_epoch_to_date(payout.arrival_date),
                "raw_data": payout.to_dict(),
            },
        )
        synced_payout_stripe_ids.append(payout.id)
        # Track the NEWEST payout timestamp for cursor (Stripe returns newest first)
        if last_created is None or payout.created > last_created:
            last_created = payout.created
        payouts_synced += 1

        # Batch commit every 10 payouts — Supabase has 2min statement timeout
        if payouts_synced % 10 == 0:
            db.commit()

        if progress_callback and payouts_synced % 20 == 0:
            progress_callback(payouts_synced, "payouts")

    # Final progress callback for remaining payouts
    if progress_callback and payouts_synced > 0:
        progress_callback(payouts_synced, "payouts_done")

    if last_created is not None:
        save_cursor(db, connection_id, "stripe_payouts", str(last_created))
    db.commit()

    logger.info("stripe_sync.payouts.done", count=payouts_synced)

    # ---- payout status refresh ---------------------------------------------
    # Runs every cycle regardless of whether the incremental fetch above found
    # anything new — non-terminal payouts go stale independently of the cursor.
    # The whole pass (including its own commits) is wrapped here: it must never
    # be able to fail the surrounding sync or a recon run that depends on it.
    try:
        refresh_summary = refresh_payout_statuses(db, connection_id, tenant_id)
    except Exception:
        logger.warning(
            "stripe_sync.status_refresh.pass_failed",
            connection_id=connection_id,
            exc_info=True,
        )
        db.rollback()
        # rollback also clears SET LOCAL — restore context for the phases below.
        from app.workers.base_task import set_tenant_context_sync as _set_ctx

        _set_ctx(db, tenant_id)
        refresh_summary = {"checked": 0, "updated": 0, "errors": 0}

    # ---- payout lines (balance transactions) ------------------------------
    logger.info("stripe_sync.payout_lines.start", connection_id=connection_id)

    for stripe_payout_id in synced_payout_stripe_ids:
        # Look up the canonical Payout UUID for FK linking
        canonical_payout = db.execute(
            select(Payout).where(
                Payout.tenant_id == tenant_id,
                Payout.dedupe_key == f"stripe:{stripe_payout_id}",
            )
        ).scalar_one_or_none()

        payout_uuid = canonical_payout.id if canonical_payout else None

        for txn in stripe.BalanceTransaction.list(payout=stripe_payout_id, limit=100).auto_paging_iter():
            upsert_canonical(
                db,
                model_class=PayoutLine,
                tenant_id=tenant_id,
                dedupe_key=f"stripe:{txn.id}",
                data={
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "dedupe_key": f"stripe:{txn.id}",
                    "source": "stripe",
                    "source_id": txn.id,
                    "payout_id": payout_uuid,
                    "line_type": txn.type,
                    "amount": Decimal(str(txn.amount / 100)),
                    "fee": Decimal(str(txn.fee / 100)),
                    "net": Decimal(str(txn.net / 100)),
                    "currency": txn.currency.upper(),
                    "description": txn.description,
                    "related_order_id": getattr(txn, "source", None),
                    "raw_data": txn.to_dict(),
                },
            )
            payout_lines_synced += 1

            # Batch commit every 10 payout lines — Supabase 2min timeout
            if payout_lines_synced % 10 == 0:
                db.commit()

        # Commit after each payout's lines
        db.commit()

    logger.info("stripe_sync.payout_lines.done", count=payout_lines_synced)

    # ---- disputes ---------------------------------------------------------
    logger.info("stripe_sync.disputes.start", connection_id=connection_id)

    cursor = load_cursor(db, connection_id, "stripe_disputes")
    list_params = {"limit": 100}
    if cursor:
        list_params["created"] = {"gt": int(cursor)}

    last_created = None

    for dispute in stripe.Dispute.list(**list_params).auto_paging_iter():
        upsert_canonical(
            db,
            model_class=Dispute,
            tenant_id=tenant_id,
            dedupe_key=f"stripe:{dispute.id}",
            data={
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "dedupe_key": f"stripe:{dispute.id}",
                "source": "stripe",
                "source_id": dispute.id,
                "amount": Decimal(str(dispute.amount / 100)),
                "currency": dispute.currency.upper(),
                "status": dispute.status,
                "reason": dispute.reason,
                "related_order_id": None,
                "related_payment_id": dispute.charge,
                "raw_data": dispute.to_dict(),
            },
        )
        # Track NEWEST dispute timestamp for cursor (Stripe returns newest first)
        if last_created is None or dispute.created > last_created:
            last_created = dispute.created
        disputes_synced += 1

    if last_created is not None:
        save_cursor(db, connection_id, "stripe_disputes", str(last_created))

    logger.info("stripe_sync.disputes.done", count=disputes_synced)

    # ---- commit & return --------------------------------------------------
    db.commit()

    summary = {
        "payouts_synced": payouts_synced,
        "payout_lines_synced": payout_lines_synced,
        "disputes_synced": disputes_synced,
        "payouts_refresh_checked": refresh_summary["checked"],
        "payouts_refresh_updated": refresh_summary["updated"],
        "payouts_refresh_errors": refresh_summary["errors"],
    }
    logger.info("stripe_sync.complete", **summary)
    return summary
