"""Stripe ingestion: sync payouts, balance transactions (payout lines), and disputes."""

import uuid
from datetime import date
from decimal import Decimal

import stripe
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.encryption import decrypt_credentials
from app.models.canonical import Dispute, Payout, PayoutLine
from app.models.connection import Connection
from app.services.ingestion.base import load_cursor, save_cursor, upsert_canonical

logger = structlog.get_logger()

# Stripe payout statuses that will never change again once reached. Anything else
# (`pending`, `in_transit`) is a candidate for the status-refresh pass below.
_NON_TERMINAL_PAYOUT_STATUSES = ("pending", "in_transit")


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
    per-row Stripe error (e.g. a deleted/inaccessible payout) is logged and
    skipped, never aborts the pass or the surrounding sync.
    """
    rows = (
        db.execute(
            select(Payout).where(
                Payout.tenant_id == tenant_id,
                Payout.source == "stripe",
                Payout.status.in_(_NON_TERMINAL_PAYOUT_STATUSES),
            )
        )
        .scalars()
        .all()
    )

    checked = 0
    updated = 0
    errors = 0

    for row in rows:
        checked += 1
        try:
            stripe_payout = stripe.Payout.retrieve(row.source_id)
        except Exception:
            errors += 1
            logger.warning(
                "stripe_sync.status_refresh.fetch_failed",
                connection_id=connection_id,
                payout_source_id=row.source_id,
                exc_info=True,
            )
            continue

        new_status = stripe_payout.status
        new_arrival_date = date.fromtimestamp(stripe_payout.arrival_date) if stripe_payout.arrival_date else None

        if new_status != row.status or new_arrival_date != row.arrival_date:
            row.status = new_status
            row.arrival_date = new_arrival_date
            updated += 1

        # Batch commit every 10 rows — Supabase 2min statement timeout
        if checked % 10 == 0:
            db.commit()

    db.commit()

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
                "arrival_date": (date.fromtimestamp(payout.arrival_date) if payout.arrival_date else None),
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
    refresh_summary = refresh_payout_statuses(db, connection_id, tenant_id)

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
