"""API-hosted collection/alert supervision, independent of Beat and Celery.

The database lock elects one API process per pass. Existing config/lease/budget
checks remain the only path to scheduling reads; this module never executes a
financial operation or steals a live lease. DB/API availability is required.
"""

import asyncio
import hashlib
from contextlib import suppress
from datetime import timedelta
from html import escape
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import engine, set_tenant_context
from app.models.audit import AuditEvent
from app.models.job import Job
from app.services import audit_service, email_service, feature_flag_service
from app.services.ops_digest import admin_emails
from app.services.transaction_ops import state_service
from app.services.transaction_ops.operational_status import operational_status
from app.services.transaction_ops.progress_clock import timestamp
from app.services.transaction_ops.scheduler import collect_due_runs

logger = structlog.get_logger(__name__)
SYSTEM_TENANT = UUID(int=0)
LOCK = 72603145270931
INTERVAL = 60
COLLECTOR_STALE = timedelta(minutes=3)
REMINDER = timedelta(hours=6)
MAX_REMINDERS_PER_DAY = 3
MAX_DELIVERY_ATTEMPTS = 3
COLLECTOR_EMAIL_STALE = timedelta(minutes=10)
HEALTH_INTERVAL = timedelta(minutes=2)
PAGE_SIZE = 50
MAX_SCOPES = 100
DELIVERY_RETRY = timedelta(minutes=5)
MAX_TENANTS = 25
MAX_RECIPIENTS = 20


def incident_items(status, *, collector_stale, now):
    items = []
    for entity in status["entities"]:
        if not entity["schedule"]["enabled"]:
            continue
        reason = None
        if entity["freshness"]["state"] == "alert":
            reason = entity["freshness"].get("detail") or entity["freshness"]["reason"]
        if reason == "monitor_unavailable" or reason == "collector_heartbeat_missing" and not collector_stale:
            # This pass just verified the shared status successfully. The
            # independent Celery digest observes stale monitor health when
            # the API monitor cannot complete a pass.
            reason = None
        if collector_stale:
            reason = "collector_heartbeat_missing" if not reason else reason
        if reason:
            items.append(
                {
                    "config_id": entity["config_id"],
                    "name": entity["name"],
                    "reason": reason,
                    "checked_through": entity["coverage"].get("checked_through"),
                    "expected_checked_through": entity["coverage"].get("expected_checked_through"),
                }
            )
    if status["truncated"]:
        items.append({"config_id": "inventory", "name": "Daily reconciliation", "reason": "scope_limit"})
    return items


def render(items):
    subject = "[Suite Studio] Daily reconciliation needs attention"
    lines = ["Daily reconciliation needs attention:", ""]
    labels = {
        "progress_stalled": "No evidence committed for at least 10 minutes",
        "queue_delayed": "Waiting for a worker for at least 15 minutes",
        "collector_heartbeat_missing": "Daily scheduler has not checked for work for at least 10 minutes",
        "monitor_unavailable": "The reconciliation monitor has stopped checking",
        "daily_scan_stopped": "Daily scan stopped",
        "coverage_overdue": "Daily coverage is overdue",
        "scope_limit": "Some daily entities exceed this monitor's check limit",
    }
    for item in items:
        lines.append(f"{item['name']}: {labels.get(item['reason'], 'Daily reconciliation needs review')}")
        if item.get("expected_checked_through"):
            lines.append(
                f"  Verified through: {item.get('checked_through') or 'none'}; "
                f"expected: {item['expected_checked_through']}"
            )
    lines.extend(
        [
            "",
            "Open Ops status to see the blocker and next action.",
            f"Review status: {email_service.FRONTEND_URL.rstrip('/')}/settings/ops-status",
        ]
    )
    body = "\n".join(lines)
    return subject, body, f'<pre style="font-family:system-ui;white-space:pre-wrap">{escape(body)}</pre>'


async def _audit(db, tenant_id, action, *, resource_id=None, payload=None):
    await set_tenant_context(db, str(tenant_id))
    event = await audit_service.log_event(
        db,
        tenant_id,
        "operations",
        action,
        actor_type="system",
        resource_type="recon_watchdog",
        resource_id=resource_id,
        payload=payload,
    )
    await db.commit()
    return event


async def notify(db, tenant_id, items, *, now, sender=None, deadline=None):
    """Durable pre-send receipt + stable provider key survive API restarts.

    A partial send retries only unfinished recipients with the exact reserved
    body. Resend retains keys 24h; our reservation expires after six hours.
    The caller holds the global session lock across commits and sends.
    An unchanged incident is capped at three reservations per rolling day.
    """
    if (
        (deadline is not None and asyncio.get_running_loop().time() + 11 >= deadline)
        or not items
        or not settings.OPS_DIGEST_EMAIL_ENABLED
    ):
        return 0
    await set_tenant_context(db, str(tenant_id))
    recipients = (await admin_emails(db, tenant_id))[:MAX_RECIPIENTS]
    fingerprint = hashlib.sha256(
        repr(
            sorted(
                (i["config_id"], i["reason"], i.get("checked_through"), i.get("expected_checked_through"))
                for i in items
            )
        ).encode()
    ).hexdigest()
    reservation = await db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.action == "recon.watchdog.alert_reserved",
            AuditEvent.resource_id == fingerprint,
        )
        .order_by(AuditEvent.timestamp.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    if reservation and now - reservation.timestamp < REMINDER:
        if now - reservation.timestamp < DELIVERY_RETRY:
            return 0
        payload = reservation.payload
    else:
        count = await db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.action == "recon.watchdog.alert_reserved",
                AuditEvent.resource_id == fingerprint,
                AuditEvent.timestamp >= now - timedelta(days=1),
            )
        )
        if not recipients or count >= MAX_REMINDERS_PER_DAY:
            return 0
        subject, body, html = render(items)
        payload = {
            "subject": subject,
            "text": body,
            "html": html,
            "recipients": [hashlib.sha256(r.encode()).hexdigest() for r in recipients],
        }
        reservation = await _audit(
            db, tenant_id, "recon.watchdog.alert_reserved", resource_id=fingerprint, payload=payload
        )
    sent = 0
    for recipient in recipients:
        if deadline is not None and asyncio.get_running_loop().time() + 11 >= deadline:
            break
        address_key = hashlib.sha256(recipient.encode()).hexdigest()
        if address_key not in payload["recipients"]:
            continue
        delivery_key = f"recon-alert/{reservation.id}/{address_key}"
        await set_tenant_context(db, str(tenant_id))
        delivered = await db.scalar(
            select(AuditEvent.id)
            .where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.action == "recon.watchdog.alert_sent",
                AuditEvent.resource_id == delivery_key,
            )
            .limit(1)
        )
        if delivered:
            continue
        last_failed, attempts = (
            await db.execute(
                select(func.max(AuditEvent.timestamp), func.count()).where(
                    AuditEvent.tenant_id == tenant_id,
                    AuditEvent.action == "recon.watchdog.alert_failed",
                    AuditEvent.resource_id == delivery_key,
                )
            )
        ).one()
        await db.commit()
        if attempts >= MAX_DELIVERY_ATTEMPTS or last_failed and now - last_failed < DELIVERY_RETRY:
            continue
        try:
            await (sender or email_service.send_recon_alert_email)(
                to_email=recipient,
                subject=payload["subject"],
                text_body=payload["text"],
                html_body=payload["html"],
                idempotency_key=delivery_key,
            )
        except Exception:
            # Never persist provider bodies, credentials or exception strings.
            await _audit(
                db,
                tenant_id,
                "recon.watchdog.alert_failed",
                resource_id=delivery_key,
                payload={"code": "delivery_failed"},
            )
            continue
        await _audit(db, tenant_id, "recon.watchdog.alert_sent", resource_id=delivery_key)
        sent += 1
    return sent


async def collect_status(db, tenant_id, now):
    entities = []
    truncated = False
    for offset in range(0, MAX_SCOPES, PAGE_SIZE):
        page = await operational_status(db, tenant_id, daily_only=True, now=now, limit=PAGE_SIZE, offset=offset)
        entities.extend(page["entities"])
        truncated = page["truncated"]
        if not truncated:
            break
    return {"entities": entities, "truncated": truncated}


async def supervise(db, *, now=None):
    """One bounded pass; DB leadership must be held by the caller."""
    now = await state_service.run_clock(db, now)
    deadline = asyncio.get_running_loop().time() + 80
    await set_tenant_context(db, str(SYSTEM_TENANT))
    last_tick = await db.scalar(
        select(func.max(AuditEvent.timestamp)).where(
            AuditEvent.tenant_id == SYSTEM_TENANT,
            AuditEvent.action == "recon.watchdog.tick",
        )
    )
    if last_tick and now - last_tick < timedelta(seconds=INTERVAL):
        return {"skipped": True}
    last_collector = await db.scalar(
        select(func.max(Job.completed_at)).where(
            Job.tenant_id == SYSTEM_TENANT,
            Job.job_type == "tasks.transaction_ops_collect_due",
            Job.status == "completed",
        )
    )
    stale = last_collector is None or now - last_collector >= COLLECTOR_STALE
    await db.commit()
    stats = {
        "collector_stale": stale,
        "fallback": None,
        "tenants": 0,
        "sent": 0,
        "tenant_failed": 0,
        "completed": False,
        "truncated": False,
    }
    try:
        if stale:
            stats["fallback"] = await collect_due_runs(db, now, scheduled_only=True)
            await db.rollback()
        tenants = await feature_flag_service.list_tenants_with_flags(db, ("celigo", "reconciliation"))
        await db.commit()
        if tenants:
            offset = (int(now.timestamp()) // INTERVAL) % len(tenants)
            stats["truncated"] = len(tenants) > MAX_TENANTS
            tenants = (tenants[offset:] + tenants[:offset])[:MAX_TENANTS]
        for tenant_id in tenants:
            if asyncio.get_running_loop().time() >= deadline:
                stats["truncated"] = True
                break
            try:
                await set_tenant_context(db, str(tenant_id))
                prior = await db.scalar(
                    select(AuditEvent)
                    .where(
                        AuditEvent.tenant_id == tenant_id,
                        AuditEvent.action == "recon.watchdog.health",
                    )
                    .order_by(AuditEvent.timestamp.desc(), AuditEvent.id.desc())
                    .limit(1)
                )
                health = prior.payload or {} if prior else {}
                observed = timestamp(health.get("observed_at")) or (prior.timestamp if prior else None)
                since = timestamp(health.get("collector_stale_since")) if health.get("collector_stale") else None
                since = (since or now) if stale else None
                status = await collect_status(db, tenant_id, now)
                if observed is None or now - observed >= HEALTH_INTERVAL or health.get("collector_stale") != stale:
                    await _audit(
                        db,
                        tenant_id,
                        "recon.watchdog.health",
                        payload={
                            "collector_stale": stale,
                            "observed_at": now.isoformat(),
                            "collector_stale_since": since.isoformat() if since else None,
                        },
                    )
                email_stale = stale and (
                    last_collector is not None
                    and now - last_collector >= COLLECTOR_EMAIL_STALE
                    or since is not None
                    and now - since >= COLLECTOR_EMAIL_STALE
                )
                items = incident_items(status, collector_stale=email_stale, now=now)
                # In-app health becomes visible immediately; collector-only mail
                # waits through ordinary warm restarts. Other terminal/stagnation
                # alerts remain prompt, with finite six-hourly reminders.
                if stale and not email_stale:
                    items = [item for item in items if item["reason"] != "collector_heartbeat_missing"]
                stats["sent"] += await notify(db, tenant_id, items, now=now, deadline=deadline)
                stats["tenants"] += 1
                await db.commit()
            except Exception:
                await db.rollback()
                stats["tenant_failed"] += 1
        stats["completed"] = True
    finally:
        if stats["tenant_failed"]:
            logger.warning("recon_watchdog_tenant_checks_failed", count=stats["tenant_failed"])
        await db.rollback()
        await _audit(db, SYSTEM_TENANT, "recon.watchdog.tick", payload=stats)
    return stats


async def release_leader(connection, locked):
    try:
        await connection.rollback()
        if locked:
            await connection.execute(select(func.pg_advisory_unlock(LOCK)))
            await connection.commit()
    except BaseException:
        # Pool rollback does not release session locks. Never return a possibly
        # locked connection to the shared API pool after failed cleanup.
        await connection.invalidate()
        raise


async def run_pass():
    async with engine.connect() as connection:
        locked = False
        try:
            locked = bool(await connection.scalar(select(func.pg_try_advisory_lock(LOCK))))
            await connection.commit()
            if not locked:
                return
            async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                async with asyncio.timeout(90):
                    return await supervise(db)
        finally:
            try:
                await asyncio.shield(release_leader(connection, locked))
            except BaseException:
                await connection.invalidate()
                raise


async def monitor():
    while True:
        await asyncio.sleep(INTERVAL)
        try:
            await run_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("recon_watchdog_pass_failed", code="monitor_unavailable")


def start():
    if settings.APP_ENV not in {"staging", "production"}:
        return None
    return asyncio.create_task(monitor(), name="recon-watchdog")


async def stop(task):
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
