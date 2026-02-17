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


def sync_stripe(db: Session, connection_id: str, tenant_id: str) -> dict:
    """Run a full incremental sync for a Stripe connection.

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
                "raw_data": dict(payout),
            },
        )
        synced_payout_stripe_ids.append(payout.id)
        last_created = payout.created
        payouts_synced += 1

    if last_created is not None:
        save_cursor(db, connection_id, "stripe_payouts", str(last_created))

    logger.info("stripe_sync.payouts.done", count=payouts_synced)

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
                    "raw_data": dict(txn),
                },
            )
            payout_lines_synced += 1

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
                "raw_data": dict(dispute),
            },
        )
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
    }
    logger.info("stripe_sync.complete", **summary)
    return summary
