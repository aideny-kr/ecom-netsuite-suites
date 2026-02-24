"""Hourly Celery task: push unsynced metered credit overage to Stripe.

Reads all tenant wallets where metered_credits_used > last_synced,
reports the delta as a Stripe usage record, and updates the watermark.
"""

import logging

from sqlalchemy import select

from app.core.config import settings
from app.models.tenant_wallet import TenantWallet
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="tasks.billing_sync", queue="sync")
def sync_metered_billing_to_stripe():
    """Push unsynced metered credits to Stripe as usage records.

    Uses a raw sync session (no tenant RLS) since this is a platform-level job.
    """
    from sqlalchemy.orm import Session

    from app.workers.base_task import sync_engine

    synced_count = 0
    error_count = 0

    with Session(sync_engine) as db:
        wallets = (
            db.execute(
                select(TenantWallet).where(
                    TenantWallet.metered_credits_used > TenantWallet.last_synced_metered_credits,
                    TenantWallet.stripe_subscription_item_id.isnot(None),
                )
            )
            .scalars()
            .all()
        )

        if not wallets:
            logger.info("billing_sync: no wallets need syncing")
            return {"synced": 0, "errors": 0}

        # Lazy import Stripe — only needed when there's work to do
        try:
            import stripe

            stripe.api_key = settings.STRIPE_API_KEY
        except ImportError:
            logger.error("billing_sync: stripe package not installed")
            return {"synced": 0, "errors": len(wallets), "detail": "stripe not installed"}

        for wallet in wallets:
            delta = wallet.metered_credits_used - wallet.last_synced_metered_credits
            if delta <= 0:
                continue

            try:
                stripe.SubscriptionItem.create_usage_record(
                    wallet.stripe_subscription_item_id,
                    quantity=delta,
                    action="increment",
                )
                wallet.last_synced_metered_credits = wallet.metered_credits_used
                synced_count += 1
                logger.info(
                    "billing_sync.reported",
                    extra={
                        "tenant_id": str(wallet.tenant_id),
                        "delta": delta,
                        "total_metered": wallet.metered_credits_used,
                    },
                )
            except Exception:
                error_count += 1
                logger.exception(
                    "billing_sync.stripe_error",
                    extra={"tenant_id": str(wallet.tenant_id)},
                )

        db.commit()

    return {"synced": synced_count, "errors": error_count}
