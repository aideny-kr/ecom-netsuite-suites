"""The cash that actually moved for an order: its saved Stripe charges and refunds.

Head-to-head, 2026-10-18 (R072103456): NetSuite refunded 2,901.39 and Solidus 2,901.38. Stripe's
own refund line, 2,901.38, settles which side is right; both agents had to reason about it. These
are the payout lines the hourly Stripe sync already saved. Nothing here calls Stripe.

On staging every saved line is the Inc account's (USD, no subsidiary), so only lines in the
order's own currency are this order's; an order in another currency has none, and says so.
"""

import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.models.canonical import PayoutLine
from app.models.transaction_ops import TransactionCase, TransactionConfig

MAX_ROWS = 200
MAX_LINES = 10
NONE = {"available": False, "reason": "no saved Stripe lines for this order"}


def _stripe_date(raw):
    """The day Stripe moved the money (its own `created`), never the day our sync stored the row."""
    created = (raw or {}).get("created") if isinstance(raw, dict) else None
    if not isinstance(created, int) or isinstance(created, bool):
        return None
    return datetime.fromtimestamp(created, tz=timezone.utc).date().isoformat()


async def order_section(db, tenant_id, order_reference, currency, *, limit=MAX_LINES):
    """The order's saved Stripe lines as an evidence section; unknown is named, never empty.

    The exact order number is matched in SQL ("Order ID: R1234" not followed by a letter or digit),
    so the row cap never hides a match behind longer numbers. The read runs in its own savepoint:
    a failure here is reported and never aborts the caller's transaction or its other evidence.
    """
    reference = str(order_reference or "")
    if not re.fullmatch(r"[A-Z]\d{4,20}", reference):
        return dict(NONE)
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency):
        return {"available": False, "reason": "order currency unknown"}
    try:
        async with db.begin_nested():
            orders = await db.scalar(
                select(func.count())
                .select_from(TransactionCase)
                .where(TransactionCase.tenant_id == tenant_id, TransactionCase.order_reference == reference)
            )
            stores = await db.scalar(
                select(func.count(func.distinct(TransactionConfig.source_connection_id))).where(
                    TransactionConfig.tenant_id == tenant_id, TransactionConfig.enabled.is_(True)
                )
            )
            if stores and stores > 1:
                # One store: an order number names one order. Two stores can share numbers, and a
                # Stripe line names no store (packet review round 3, R3).
                return {"available": False, "reason": "several stores; Stripe lines carry no store to attribute them"}
            if orders and orders > 1:
                # Stripe lines carry no store or entity: a number two orders share names neither.
                return {
                    "available": False,
                    "reason": "order number shared by more than one order; Stripe lines cannot be attributed",
                }
            rows = (
                (
                    await db.execute(
                        select(PayoutLine)
                        .where(
                            PayoutLine.tenant_id == tenant_id,
                            PayoutLine.source == "stripe",
                            PayoutLine.currency == currency.upper(),
                            # Stripe writes "Order ID: <number>-<payment code>", and a refund wraps it as
                            # "REFUND FOR CHARGE (... Order ID: <number>-<code>)". Nothing else follows,
                            # so a suffixed order (R123456789-EU-<code>) never matches the plain number.
                            # Staging, 2026-10-19: 439,433 of 439,452 order lines have this shape.
                            PayoutLine.description.op("~")(f"Order ID: {reference}-[A-Za-z0-9]{{4,16}}\\)?$"),
                        )
                        .order_by(PayoutLine.created_at, PayoutLine.source_id)
                        .limit(MAX_ROWS)
                    )
                )
                .scalars()
                .all()
            )
    except SQLAlchemyError:
        return {"available": False, "reason": "stripe lines unreadable"}
    lines = [
        {
            "type": row.line_type,
            "amount": str(row.amount),
            "fee": str(row.fee),
            "currency": row.currency,
            "date": _stripe_date(row.raw_data),
            "id": row.source_id,
        }
        for row in rows
    ]
    if not lines:
        return dict(NONE)
    return {
        "available": True,
        "matched_by": "order number and payment code in the Stripe description; the tenant has one store",
        "lines": lines[:limit],
        "truncated": len(lines) > limit,
    }
