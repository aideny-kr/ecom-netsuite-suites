"""Shopify ingestion: sync orders, refunds, and payment transactions."""

import uuid
from decimal import Decimal

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.encryption import decrypt_credentials
from app.models.canonical import Order, Payment, Refund
from app.models.connection import Connection
from app.services.ingestion.base import load_cursor, save_cursor, upsert_canonical

logger = structlog.get_logger()

_SHOPIFY_API_VERSION = "2024-01"


def _parse_link_header(link_header: str | None) -> str | None:
    """Extract the 'next' page URL from a Shopify Link header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            url = part.split(";")[0].strip().strip("<>")
            return url
    return None


def sync_shopify(db: Session, connection_id: str, tenant_id: str) -> dict:
    """Run a full incremental sync for a Shopify connection.

    Returns a summary dict with counts of records synced.
    """
    # ---- bootstrap --------------------------------------------------------
    connection = db.execute(select(Connection).where(Connection.id == connection_id)).scalar_one()

    creds = decrypt_credentials(connection.encrypted_credentials)
    access_token = creds["access_token"]
    shop_domain = creds["shop_domain"]

    base_url = f"https://{shop_domain}/admin/api/{_SHOPIFY_API_VERSION}"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }

    orders_synced = 0
    refunds_synced = 0
    payments_synced = 0

    # Keep track of orders we pulled so we can process refunds & transactions
    fetched_orders: list[dict] = []

    # ---- orders -----------------------------------------------------------
    logger.info("shopify_sync.orders.start", connection_id=connection_id)

    cursor = load_cursor(db, connection_id, "shopify_orders")
    params: dict = {"status": "any", "limit": 250}
    if cursor:
        params["updated_at_min"] = cursor

    last_updated_at: str | None = None

    with httpx.Client(headers=headers, timeout=30) as client:
        url: str | None = f"{base_url}/orders.json"

        while url:
            resp = client.get(url, params=params if url.endswith("/orders.json") else None)
            resp.raise_for_status()
            orders_data = resp.json().get("orders", [])

            for order in orders_data:
                upsert_canonical(
                    db,
                    model_class=Order,
                    tenant_id=tenant_id,
                    dedupe_key=f"shopify:{order['id']}",
                    data={
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "dedupe_key": f"shopify:{order['id']}",
                        "source": "shopify",
                        "source_id": str(order["id"]),
                        "order_number": str(order["order_number"]),
                        "customer_email": order.get("email"),
                        "currency": order["currency"].upper(),
                        "total_amount": Decimal(order["total_price"]),
                        "subtotal": Decimal(order.get("subtotal_price", "0")),
                        "tax_amount": Decimal(order.get("total_tax", "0")),
                        "discount_amount": Decimal(order.get("total_discounts", "0")),
                        "status": order.get("financial_status", "unknown"),
                        "raw_data": order,
                    },
                )
                fetched_orders.append(order)
                last_updated_at = order.get("updated_at", last_updated_at)
                orders_synced += 1

            # Follow pagination via Link header
            url = _parse_link_header(resp.headers.get("Link"))

    if last_updated_at is not None:
        save_cursor(db, connection_id, "shopify_orders", last_updated_at)

    logger.info("shopify_sync.orders.done", count=orders_synced)

    # ---- refunds ----------------------------------------------------------
    logger.info("shopify_sync.refunds.start", connection_id=connection_id)

    for order in fetched_orders:
        order_refunds = order.get("refunds", [])
        if not order_refunds:
            continue

        # Look up canonical Order UUID for FK
        canonical_order = db.execute(
            select(Order).where(
                Order.tenant_id == tenant_id,
                Order.dedupe_key == f"shopify:{order['id']}",
            )
        ).scalar_one_or_none()

        order_uuid = canonical_order.id if canonical_order else None

        for refund in order_refunds:
            # Sum refund line items to get total refund amount
            refund_amount = Decimal("0")
            for line_item in refund.get("refund_line_items", []):
                refund_amount += Decimal(str(line_item.get("subtotal", "0")))

            upsert_canonical(
                db,
                model_class=Refund,
                tenant_id=tenant_id,
                dedupe_key=f"shopify:refund:{refund['id']}",
                data={
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "dedupe_key": f"shopify:refund:{refund['id']}",
                    "source": "shopify",
                    "source_id": str(refund["id"]),
                    "order_id": order_uuid,
                    "amount": refund_amount,
                    "currency": order["currency"].upper(),
                    "reason": refund.get("note"),
                    "status": "completed",
                    "raw_data": refund,
                },
            )
            refunds_synced += 1

    logger.info("shopify_sync.refunds.done", count=refunds_synced)

    # ---- payments (transactions) ------------------------------------------
    logger.info("shopify_sync.payments.start", connection_id=connection_id)

    with httpx.Client(headers=headers, timeout=30) as client:
        for order in fetched_orders:
            shopify_order_id = order["id"]

            # Look up canonical Order UUID for FK
            canonical_order = db.execute(
                select(Order).where(
                    Order.tenant_id == tenant_id,
                    Order.dedupe_key == f"shopify:{shopify_order_id}",
                )
            ).scalar_one_or_none()

            order_uuid = canonical_order.id if canonical_order else None

            txn_url = f"{base_url}/orders/{shopify_order_id}/transactions.json"
            resp = client.get(txn_url)
            resp.raise_for_status()
            transactions = resp.json().get("transactions", [])

            for txn in transactions:
                upsert_canonical(
                    db,
                    model_class=Payment,
                    tenant_id=tenant_id,
                    dedupe_key=f"shopify:txn:{txn['id']}",
                    data={
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "dedupe_key": f"shopify:txn:{txn['id']}",
                        "source": "shopify",
                        "source_id": str(txn["id"]),
                        "order_id": order_uuid,
                        "amount": Decimal(txn["amount"]),
                        "currency": txn.get("currency", "USD").upper(),
                        "status": txn["status"],
                        "payment_method": txn.get("gateway"),
                        "raw_data": txn,
                    },
                )
                payments_synced += 1

    logger.info("shopify_sync.payments.done", count=payments_synced)

    # ---- commit & return --------------------------------------------------
    db.commit()

    summary = {
        "orders_synced": orders_synced,
        "refunds_synced": refunds_synced,
        "payments_synced": payments_synced,
    }
    logger.info("shopify_sync.complete", **summary)
    return summary
