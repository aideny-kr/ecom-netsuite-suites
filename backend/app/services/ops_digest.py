"""Daily ops digest: the outcomes nobody is watching at 03:00, delivered to a person.

Unattended means nobody is watching by definition. Every active tenant gets exactly one
durable audit row per run (action ``ops.digest``) carrying the counts and the ids of what
needs a human, whether or not email is configured, so a missing row is itself a signal.
Email goes out only when there is something to report and the tenant has an admin.

What counts as needing a human, per tenant:

* transaction-ops operations that ended ``unknown`` or ``failed`` since the last digest;
* recovery runs whose settlement verdict is unverified or shows a difference;
* write-confirmation cards left ``indeterminate`` (a standing condition: it stays in every
  digest until a person resolves it), or ``executing`` longer than one tool ceiling plus a
  grace period (a worker died mid-write);
* connections currently in ``error`` (a standing condition, not windowed);
* jobs that failed since the last digest.

The digest is read-only: it never retries, resets or resends anything.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html import escape
from uuid import UUID

from sqlalchemy import func, or_, select

from app.core.config import settings
from app.core.database import set_tenant_context
from app.models.audit import AuditEvent
from app.models.chat import ChatMessage
from app.models.connection import Connection
from app.models.job import Job
from app.models.tenant import Tenant
from app.models.transaction_ops import TransactionOperation, TransactionRun
from app.models.user import Role, User, UserRole
from app.services import audit_service, email_service

logger = logging.getLogger(__name__)

ACTION = "ops.digest"
TASK_NAME = "tasks.ops_digest"
WINDOW = timedelta(hours=24)
ROW_LIMIT = 50  # ids carried per category in the audit row; counts stay exact
TENANT_LIMIT = 500  # tenants per run, longest-waiting first; more than this ends the run with reason "budget"
STALE_GRACE = timedelta(minutes=10)
# `settlement.status` is written as unverified | succeeded | difference (settlement.py,
# accounting_recheck.py). "not_verified" is the sibling balance/cash_settlement value and
# never appears here.
SETTLEMENT_NEEDS_HUMAN = ("unverified", "difference")
# One source of truth for the category names: the labels. collect() asserts against it.
_LABELS = {
    "operations": "Transaction operations ended unknown or failed",
    "rechecks": "Recovery runs whose settlement needs review",
    "cards": "Write confirmations left indeterminate or stuck executing",
    "connections": "Connections in error",
    "jobs": "Jobs that failed",
}
CATEGORIES = tuple(_LABELS)


def _stale_after() -> timedelta:
    """One MCP tool ceiling plus grace: a card still ``executing`` past this has no live caller."""
    from app.services.mcp_client_service import _tool_timeout_seconds

    return timedelta(seconds=_tool_timeout_seconds("ns_createRecord")) + STALE_GRACE


# A digest counts as delivered when a person could have read it: sent, or nothing to
# report, or email deliberately off (the audit row is then the digest). A row whose
# email failed or had no recipient does not move the window, so the incidents it
# carried are reported again next time instead of ageing out unseen.
DELIVERED = ("sent", "nothing_to_report", "disabled")


async def last_delivered_at(db) -> dict[UUID, datetime]:
    """Per tenant, when the last digest that reached (or could reach) a person was written.

    Runs once for every tenant before any tenant context is set. audit_events carries a
    row-level policy without FORCE, and the worker connects as the owning role, which
    bypasses it; a role subject to the policy would not get an empty map here, it would
    raise (the tenant function has no setting to read) and the run would fail loudly.
    """
    rows = await db.execute(
        select(AuditEvent.tenant_id, func.max(AuditEvent.timestamp))
        .where(AuditEvent.action == ACTION, AuditEvent.payload["delivery"].astext.in_(DELIVERED))
        .group_by(AuditEvent.tenant_id)
    )
    return dict(rows.all())


async def undelivered_since(db) -> dict[UUID, datetime]:
    """Per tenant, the earliest window start among digests nobody received.

    A tenant that has never had a delivered digest must not get a fresh 24-hour window
    every night: the first digest a person does receive has to cover everything the
    failed ones carried, so the window floor is the oldest undelivered digest's own start.
    """
    rows = await db.execute(
        select(AuditEvent.tenant_id, func.min(AuditEvent.payload["since"].astext))
        .where(AuditEvent.action == ACTION, AuditEvent.payload["delivery"].astext.notin_(DELIVERED))
        .group_by(AuditEvent.tenant_id)
    )
    return {tenant_id: datetime.fromisoformat(value) for tenant_id, value in rows.all() if value}


async def _category(db, stmt, order_by):
    """Exact count plus a bounded id sample for one category.

    Most categories are empty on most nights, so the sample is read first and the
    separate count runs only when the sample overflowed the row limit.
    """
    ids = [str(value) for value in await db.scalars(stmt.order_by(order_by).limit(ROW_LIMIT + 1))]
    if len(ids) <= ROW_LIMIT:
        return len(ids), ids
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    return int(total or 0), ids[:ROW_LIMIT]


async def collect(db, tenant_id: UUID, *, now: datetime, since: datetime) -> dict:
    """Read-only inventory of what needs a human for one tenant."""
    stale_before = now - _stale_after()
    queries = {
        "operations": (
            select(TransactionOperation.id).where(
                TransactionOperation.tenant_id == tenant_id,
                TransactionOperation.status.in_(("unknown", "failed")),
                TransactionOperation.updated_at >= since,
            ),
            TransactionOperation.updated_at.desc(),
        ),
        "rechecks": (
            select(TransactionRun.id).where(
                TransactionRun.tenant_id == tenant_id,
                TransactionRun.origin == "recovery",
                TransactionRun.status == "finished",
                TransactionRun.updated_at >= since,
                TransactionRun.progress_json["settlement"]["status"].astext.in_(SETTLEMENT_NEEDS_HUMAN),
            ),
            TransactionRun.updated_at.desc(),
        ),
        "cards": (
            select(ChatMessage.id).where(
                ChatMessage.tenant_id == tenant_id,
                ChatMessage.structured_output["type"].astext == "write_confirmation",
                or_(
                    # Standing, never windowed by `since`: an indeterminate card's updated_at
                    # stops moving once recovery gives up on it, and a window keyed on the
                    # last digest would report it exactly once and then never again.
                    ChatMessage.structured_output["status"].astext == "indeterminate",
                    (ChatMessage.structured_output["status"].astext == "executing")
                    & (ChatMessage.updated_at < stale_before),
                ),
            ),
            ChatMessage.updated_at.desc(),
        ),
        "connections": (
            select(Connection.id).where(Connection.tenant_id == tenant_id, Connection.status == "error"),
            Connection.updated_at.desc(),
        ),
        "jobs": (
            select(Job.id).where(
                Job.tenant_id == tenant_id,
                Job.status == "failed",
                Job.job_type != TASK_NAME,
                func.coalesce(Job.completed_at, Job.updated_at) >= since,
            ),
            func.coalesce(Job.completed_at, Job.updated_at).desc(),
        ),
    }
    if tuple(queries) != CATEGORIES:  # a plain check, so `python -O` cannot strip it
        raise RuntimeError(f"digest categories {tuple(queries)} drifted from labels {CATEGORIES}")
    counts, ids, truncated = {}, {}, {}
    for name in CATEGORIES:
        stmt, order_by = queries[name]
        counts[name], ids[name] = await _category(db, stmt, order_by)
        truncated[name] = counts[name] > len(ids[name])
    return {"counts": counts, "ids": ids, "truncated": truncated}


def _epoch(value: datetime | None) -> float:
    if value is None:
        return 0.0
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()


async def tenants_due(db, *, tenant_ids: list[UUID] | None = None) -> tuple[list[Tenant], bool, dict]:
    """Active tenants in the order this run should serve them, whether the cap cut it,
    and each tenant's last delivered digest time (the start of its next window).

    Tenants that have never had a delivered digest come first, then the longest-waiting.
    A fixed cap over a fixed order would leave the same tenants beyond it every single
    run. With explicit ``tenant_ids`` the caller's order is kept and the cap does not apply.
    """
    last = await last_delivered_at(db)
    floor = await undelivered_since(db)
    for tenant_id, since in floor.items():
        # Only when nothing was ever delivered: a delivered digest is always the newer boundary.
        last.setdefault(tenant_id, since)
    if tenant_ids is not None:
        rows = {t.id: t for t in await db.scalars(select(Tenant).where(Tenant.id.in_(tenant_ids)))}
        return [rows[i] for i in tenant_ids if i in rows], False, last
    tenants = list(await db.scalars(select(Tenant).where(Tenant.is_active.is_(True))))
    delivered = await last_delivered_at(db) if floor else last
    tenants.sort(key=lambda t: (delivered.get(t.id) is not None, _epoch(delivered.get(t.id)), str(t.id)))
    return tenants[:TENANT_LIMIT], len(tenants) > TENANT_LIMIT, last


async def admin_emails(db, tenant_id: UUID) -> list[str]:
    rows = await db.scalars(
        select(User.email)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            User.tenant_id == tenant_id,
            UserRole.tenant_id == tenant_id,
            User.is_active.is_(True),
            Role.name == "admin",
        )
        .order_by(User.email)
    )
    return list(dict.fromkeys(rows))


def render(tenant_name: str, digest: dict, *, since: datetime, until: datetime) -> tuple[str, str, str]:
    total = sum(digest["counts"].values())
    subject = f"[Suite Studio] Ops digest for {tenant_name}: {total} item(s) need attention"
    lines = [
        f"Ops digest for {tenant_name}",
        f"Window: {since.isoformat()} to {until.isoformat()}",
        "",
    ]
    html = [
        f"<h2>Ops digest for {escape(tenant_name)}</h2>",
        f"<p>Window: {since.isoformat()} to {until.isoformat()}</p>",
    ]
    for name in CATEGORIES:
        count = digest["counts"][name]
        if not count:
            continue
        shown = digest["ids"][name]
        suffix = f" (first {len(shown)} of {count})" if digest["truncated"][name] else ""
        lines.append(f"{_LABELS[name]}: {count}{suffix}")
        lines.extend(f"  - {value}" for value in shown)
        lines.append("")
        html.append(f"<h3>{escape(_LABELS[name])}: {count}{escape(suffix)}</h3>")
        html.append("<ul>" + "".join(f"<li><code>{escape(value)}</code></li>" for value in shown) + "</ul>")
    lines.append("This digest is read-only. Nothing was retried, reset or resent.")
    html.append("<p>This digest is read-only. Nothing was retried, reset or resent.</p>")
    return subject, "\n".join(lines), "\n".join(html)


async def run_ops_digest(
    db,
    *,
    now: datetime | None = None,
    sender=None,
    tenant_ids: list[UUID] | None = None,
    window: timedelta = WINDOW,
) -> dict:
    """One digest per active tenant. Returns run stats with a termination reason.

    ``sender`` is ``email_service.send_ops_digest_email`` unless injected. A tenant's
    audit row is written whether the email is sent, disabled, undeliverable or failed.
    """
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError("An aware clock is required")
    send = sender or email_service.send_ops_digest_email
    stats = {"tenants": 0, "sent": 0, "tenant_failed": 0, "truncated": False, "termination_reason": "done"}

    tenants, stats["truncated"], last = await tenants_due(db, tenant_ids=tenant_ids)

    for tenant in tenants:
        tenant_id = tenant.id
        try:
            await set_tenant_context(db, str(tenant_id))
            since = last.get(tenant_id) or (now - window)
            digest = await collect(db, tenant_id, now=now, since=since)
            total = sum(digest["counts"].values())
            # Recipients are only looked up when there is something to send them.
            recipients = await admin_emails(db, tenant_id) if total else []
            failed_recipients = []
            if not total:
                delivery = "nothing_to_report"
            elif not settings.OPS_DIGEST_EMAIL_ENABLED:
                delivery = "disabled"
            elif not recipients:
                delivery = "no_recipient"
            else:
                subject, text_body, html_body = render(tenant.name, digest, since=since, until=now)
                for to_email in recipients:
                    try:
                        await send(to_email=to_email, subject=subject, text_body=text_body, html_body=html_body)
                    except Exception:
                        logger.exception("ops_digest.send_failed", extra={"tenant_id": str(tenant_id)})
                        failed_recipients.append(to_email)
                if not failed_recipients:
                    delivery = "sent"
                    stats["sent"] += 1
                elif len(failed_recipients) < len(recipients):
                    delivery = "partial"
                else:
                    delivery = "failed"
            await audit_service.log_event(
                db,
                tenant_id,
                category="ops",
                action=ACTION,
                actor_type="system",
                resource_type="tenant",
                resource_id=str(tenant_id),
                payload={
                    "since": since.isoformat(),
                    "until": now.isoformat(),
                    **digest,
                    "recipients": recipients,
                    "failed_recipients": failed_recipients,
                    "delivery": delivery,
                    "financial_writes": 0,
                },
                status="error" if failed_recipients else "success",
            )
            await db.commit()
            stats["tenants"] += 1
        except Exception:
            await db.rollback()
            stats["tenant_failed"] += 1
            logger.exception("ops_digest.tenant_failed", extra={"tenant_id": str(tenant_id)})

    if stats["truncated"]:
        stats["termination_reason"] = "budget"
    elif stats["tenant_failed"]:
        stats["termination_reason"] = "error"
    return stats
