"""The cash that actually moved for an order: its saved Stripe charges and refunds.

Head-to-head, 2026-10-18 (R072103456): NetSuite refunded 2,901.39 and Solidus 2,901.38. Stripe's
own refund line, 2,901.38, settles which side is right; both agents had to reason about it. These
are the payout lines the hourly Stripe sync already saved. Nothing here calls Stripe.
"""

import re

from sqlalchemy import select

from app.models.canonical import PayoutLine

# Stripe lines for one order are a handful; a reader keeps fewer and marks the cut.
MAX_ROWS = 200


async def order_lines(db, tenant_id, order_reference):
    """Saved Stripe payout lines whose description names exactly this order. Never reads Stripe."""
    reference = str(order_reference or "")
    if not re.fullmatch(r"[A-Z]\d{4,20}", reference):
        return []
    # Stripe writes "Framework Marketplace Order ID: R072103456-XC8PUK5B": the number then a
    # payment suffix. The LIKE narrows; the pattern below rejects a longer number (R0721034560).
    exact = re.compile(rf"Order ID: {reference}(?![0-9A-Za-z])")
    rows = (
        await db.execute(
            select(PayoutLine)
            .where(
                PayoutLine.tenant_id == tenant_id,
                PayoutLine.source == "stripe",
                PayoutLine.description.like(f"%Order ID: {reference}%"),
            )
            .order_by(PayoutLine.created_at, PayoutLine.source_id)
            .limit(MAX_ROWS)
        )
    ).scalars()
    return [
        {
            "type": row.line_type,
            "amount": str(row.amount),
            "fee": str(row.fee),
            "currency": row.currency,
            "date": row.created_at.date().isoformat() if row.created_at else None,
            "id": row.source_id,
        }
        for row in rows
        if exact.search(row.description or "")
    ]
