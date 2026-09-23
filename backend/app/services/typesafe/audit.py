"""Persist a Jev-versus-current-path comparison without ever endangering the caller.

Both call sites (the recon worker and the chat agent) share a session that the
caller commits, so the insert runs in a savepoint and every failure is swallowed
with a warning: losing a measurement is acceptable, breaking a proposal or a chat
turn to record one is not. Payloads carry decisions and timings, never tenant text.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def record_comparison(db, *, tenant_id, category: str, action: str, payload: dict, **identifiers) -> None:
    from app.services.audit_service import log_event

    try:
        async with db.begin_nested():
            await log_event(
                db=db,
                tenant_id=tenant_id,
                category=category,
                action=action,
                actor_type="system",
                payload=payload,
                **identifiers,
            )
    except Exception:
        logger.warning("jev.comparison_not_recorded", extra={"action": action}, exc_info=True)
