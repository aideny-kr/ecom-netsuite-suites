"""Periodic Celery task: verify Stripe API key validity.

Runs every 15 minutes via beat schedule. Calls stripe.Account.retrieve()
for each Stripe connection and updates status accordingly.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="tasks.stripe_health_check", queue="sync")
def stripe_health_check():
    """Verify Stripe API key validity for all Stripe connections."""
    import stripe

    from app.core.encryption import decrypt_credentials
    from app.models.connection import Connection
    from app.workers.base_task import sync_engine

    now = datetime.now(timezone.utc)
    stats = {"checked": 0, "healthy": 0, "invalid": 0, "errors": 0}

    with Session(sync_engine) as db:
        connections = (
            db.execute(
                select(Connection).where(
                    Connection.provider == "stripe",
                    Connection.status != "revoked",
                )
            )
            .scalars()
            .all()
        )

        for conn in connections:
            stats["checked"] += 1
            try:
                creds = decrypt_credentials(conn.encrypted_credentials)
                api_key = creds.get("api_key", "")
                if not api_key:
                    conn.status = "error"
                    conn.error_reason = "No API key stored"
                    conn.last_health_check_at = now
                    stats["invalid"] += 1
                    continue

                # Validate by calling Stripe API
                stripe.api_key = api_key
                stripe.Account.retrieve()

                # Success — mark healthy
                conn.last_health_check_at = now
                if conn.status in ("error", "offline"):
                    conn.status = "active"
                    conn.error_reason = None
                stats["healthy"] += 1

            except stripe.error.AuthenticationError:
                conn.status = "error"
                conn.error_reason = "Stripe API key is invalid or revoked"
                conn.last_health_check_at = now
                stats["invalid"] += 1
                logger.warning(
                    "stripe_health_check.auth_failed",
                    extra={"connection_id": str(conn.id)},
                )

            except Exception:
                conn.last_health_check_at = now
                stats["errors"] += 1
                logger.exception(
                    "stripe_health_check.error",
                    extra={"connection_id": str(conn.id)},
                )

        db.commit()

    logger.info("stripe_health_check.completed", extra=stats)
    return stats
