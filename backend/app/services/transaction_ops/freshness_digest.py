"""Bounded daily email inventory using the same saved freshness as Ops status."""

import hashlib
import json

from sqlalchemy import func, select

from app.models.audit import AuditEvent
from app.services.transaction_ops.operational_status import operational_status

MAX_SCOPES = 500
PAGE_SIZE = 50


async def claim_digest(db, tenant_id):
    """Serialize digest delivery for a tenant without locking its customer rows.

    The transaction-scoped advisory lock releases on commit/rollback. A duplicate
    worker skips a busy tenant rather than waiting on an unbounded network send.
    """
    key = int.from_bytes(hashlib.sha256(f"ops.digest:{tenant_id}".encode()).digest()[:8], signed=True)
    return bool(await db.scalar(select(func.pg_try_advisory_xact_lock(key))))


async def collect_freshness(db, tenant_id, *, now):
    """Notify once per unchanged coverage window successfully emailed to admins.

    Failed, partial and disabled deliveries never suppress the next attempt.
    All current alert keys are carried forward, including already notified ones,
    so a quiet audit row or an unrelated incident cannot reset deduplication.
    Counts cover checked scopes only; hitting the cap is itself an attention item.
    """
    previous = await db.scalar(
        select(AuditEvent.payload)
        .where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.action == "ops.digest",
            AuditEvent.payload["delivery"].astext == "sent",
        )
        .order_by(AuditEvent.timestamp.desc(), AuditEvent.payload["until"].astext.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    notified = set((previous or {}).get("freshness_keys", []))
    alerts, keys, checked, truncated = [], [], 0, False
    for offset in range(0, MAX_SCOPES, PAGE_SIZE):
        page = await operational_status(db, tenant_id, daily_only=True, limit=PAGE_SIZE, offset=offset, now=now)
        checked += len(page["entities"])
        for entity in page["entities"]:
            if entity["freshness"]["state"] != "alert":
                continue
            coverage = entity["coverage"]
            key = hashlib.sha256(
                json.dumps(
                    [entity["config_id"], coverage["expected_until"], coverage.get("completed_until")],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            keys.append(key)
            if key in notified:
                continue
            alerts.append(
                {
                    "config_id": entity["config_id"],
                    "name": entity["name"],
                    "reason": entity["freshness"]["reason"],
                    "checked_through": coverage.get("checked_through"),
                    "expected_checked_through": coverage["expected_checked_through"],
                    "deadline_at": entity["freshness"]["deadline_at"],
                }
            )
        truncated = page["truncated"]
        if not truncated:
            break
    return {"alerts": alerts, "keys": keys, "scopes_checked": checked, "scope_truncated": truncated}
