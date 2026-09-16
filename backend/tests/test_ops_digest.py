"""The daily ops digest: unattended outcomes reach a person, and the digest exists as a
durable audit row even when email is off. Absence of a digest row is a failure signal,
so every active tenant gets one row per run, including an empty one.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.connection import Connection
from app.models.job import Job
from app.services import ops_digest
from app.workers.base_task import InstrumentedTask
from app.workers.tasks import ops_digest as task_module
from tests import test_transaction_ops_executor as execution_fixtures
from tests import test_transaction_ops_recovery as recovery_fixtures
from tests.conftest import create_test_tenant, create_test_user
from tests.test_transaction_ops_executor import operation

execution_case = execution_fixtures.execution_case
unknown_case = recovery_fixtures.unknown_case


async def _seed_incidents(db, tenant_id, now):
    connection = Connection(
        tenant_id=tenant_id,
        provider="netsuite",
        label="NetSuite",
        status="error",
        error_reason="OAuth token expired",
        encrypted_credentials="not-a-secret",
    )
    job = Job(
        tenant_id=tenant_id,
        job_type="tasks.stripe_sync",
        status="failed",
        error_message="boom",
        started_at=now - timedelta(minutes=5),
        completed_at=now - timedelta(minutes=4),
    )
    db.add_all([connection, job])
    await db.flush()
    return connection, job


async def _digest_rows(db, tenant_id):
    # Ordered by the window end each row reports: inside one test transaction every row
    # shares the transaction's now(), and primary keys are random, so nothing else orders them.
    return list(
        await db.scalars(
            select(AuditEvent)
            .where(AuditEvent.tenant_id == tenant_id, AuditEvent.action == "ops.digest")
            .order_by(AuditEvent.payload["until"].astext)
        )
    )


async def test_digest_lists_unknown_operation_error_connection_and_failed_job_and_sends_once(db, unknown_case):
    now = datetime.now(timezone.utc)
    tenant_id = unknown_case.actor.tenant_id
    row = await operation(db, unknown_case)
    assert row.status == "unknown"
    connection, job = await _seed_incidents(db, tenant_id, now)
    sender = AsyncMock()

    stats = await ops_digest.run_ops_digest(db, now=now, sender=sender, tenant_ids=[tenant_id])

    rows = await _digest_rows(db, tenant_id)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["financial_writes"] == 0
    assert payload["counts"]["operations"] == 1 and str(row.id) in payload["ids"]["operations"]
    assert payload["counts"]["connections"] == 1 and str(connection.id) in payload["ids"]["connections"]
    assert payload["counts"]["jobs"] == 1 and str(job.id) in payload["ids"]["jobs"]
    assert payload["delivery"] == "sent"
    assert payload["recipients"] == [unknown_case.actor.email]
    sender.assert_awaited_once()
    assert sender.call_args.kwargs["to_email"] == unknown_case.actor.email
    assert str(row.id) in sender.call_args.kwargs["text_body"]
    assert stats["tenants"] == 1 and stats["sent"] == 1 and stats["termination_reason"] == "done"


async def test_digest_row_is_written_when_email_is_disabled(db, unknown_case, monkeypatch):
    monkeypatch.setattr(settings, "OPS_DIGEST_EMAIL_ENABLED", False)
    now = datetime.now(timezone.utc)
    tenant_id = unknown_case.actor.tenant_id
    await operation(db, unknown_case)
    await _seed_incidents(db, tenant_id, now)
    sender = AsyncMock()

    stats = await ops_digest.run_ops_digest(db, now=now, sender=sender, tenant_ids=[tenant_id])

    rows = await _digest_rows(db, tenant_id)
    assert len(rows) == 1
    assert rows[0].payload["counts"]["operations"] == 1
    assert rows[0].payload["delivery"] == "disabled"
    sender.assert_not_awaited()
    assert stats["sent"] == 0 and stats["termination_reason"] == "done"


async def test_digest_window_excludes_old_incidents_but_keeps_current_error_connections(db, unknown_case):
    tenant_id = unknown_case.actor.tenant_id
    await operation(db, unknown_case)
    connection, _ = await _seed_incidents(db, tenant_id, datetime.now(timezone.utc))
    sender = AsyncMock()
    far_future = datetime.now(timezone.utc) + timedelta(days=3)

    await ops_digest.run_ops_digest(db, now=far_future, sender=sender, tenant_ids=[tenant_id])

    payload = (await _digest_rows(db, tenant_id))[0].payload
    assert payload["counts"]["operations"] == 0 and payload["counts"]["jobs"] == 0
    assert payload["counts"]["connections"] == 1 and str(connection.id) in payload["ids"]["connections"]
    assert payload["delivery"] == "sent"


async def test_empty_tenant_still_gets_a_digest_row_and_no_email(db):
    tenant = await create_test_tenant(db, name="Quiet Corp")
    sender = AsyncMock()

    await ops_digest.run_ops_digest(db, now=datetime.now(timezone.utc), sender=sender, tenant_ids=[tenant.id])

    rows = await _digest_rows(db, tenant.id)
    assert len(rows) == 1
    assert all(count == 0 for count in rows[0].payload["counts"].values())
    assert rows[0].payload["delivery"] == "nothing_to_report"
    sender.assert_not_awaited()


async def test_tenant_without_an_admin_records_no_recipient(db):
    tenant = await create_test_tenant(db, name="Headless Corp")
    now = datetime.now(timezone.utc)
    await _seed_incidents(db, tenant.id, now)
    sender = AsyncMock()

    await ops_digest.run_ops_digest(db, now=now, sender=sender, tenant_ids=[tenant.id])

    payload = (await _digest_rows(db, tenant.id))[0].payload
    assert payload["counts"]["jobs"] == 1
    assert payload["delivery"] == "no_recipient" and payload["recipients"] == []
    sender.assert_not_awaited()


def test_daily_task_is_registered_instrumented_and_scheduled():
    app = task_module.celery_app
    assert "app.workers.tasks.ops_digest" in app.conf.include
    task = task_module.ops_digest_task
    assert task.name == "tasks.ops_digest"
    assert isinstance(task, InstrumentedTask)
    assert task.max_retries == 0
    assert task.time_limit <= 600
    entries = [entry for entry in app.conf.beat_schedule.values() if entry["task"] == "tasks.ops_digest"]
    assert len(entries) == 1


async def test_partial_send_failure_names_the_failed_recipient_and_keeps_the_row(db):
    tenant = await create_test_tenant(db, name="Two Admins Corp")
    good, _ = await create_test_user(db, tenant, email="good@example.com", role_name="admin")
    bad, _ = await create_test_user(db, tenant, email="bad@example.com", role_name="admin")
    now = datetime.now(timezone.utc)
    await _seed_incidents(db, tenant.id, now)

    async def sender(*, to_email, **_):
        if to_email == bad.email:
            raise RuntimeError("provider rejected")

    await ops_digest.run_ops_digest(db, now=now, sender=sender, tenant_ids=[tenant.id])

    row = (await _digest_rows(db, tenant.id))[0]
    assert row.payload["delivery"] == "partial"
    assert row.payload["recipients"] == [bad.email, good.email]
    assert row.payload["failed_recipients"] == [bad.email]
    assert row.status == "error"


async def test_an_unresolved_indeterminate_card_stays_in_every_digest(db, unknown_case):
    """A standing condition: the card's updated_at stops moving once recovery gives up, so a
    window keyed on the previous digest would report it once and then never again."""
    from app.models.chat import ChatMessage, ChatSession

    tenant_id = unknown_case.actor.tenant_id
    first_run = datetime.now(timezone.utc) - timedelta(days=2)
    await ops_digest.run_ops_digest(db, now=first_run, sender=AsyncMock(), tenant_ids=[tenant_id])
    session = ChatSession(tenant_id=tenant_id, user_id=unknown_case.actor.id)
    db.add(session)
    await db.flush()
    card = ChatMessage(
        tenant_id=tenant_id,
        session_id=session.id,
        role="assistant",
        content="",
        structured_output={"type": "write_confirmation", "status": "indeterminate", "mutation_type": "create"},
        created_at=first_run - timedelta(hours=1),
        updated_at=first_run - timedelta(hours=1),  # before the previous digest, never touched since
    )
    db.add(card)
    await db.flush()
    sender = AsyncMock()

    await ops_digest.run_ops_digest(db, now=datetime.now(timezone.utc), sender=sender, tenant_ids=[tenant_id])

    payload = (await _digest_rows(db, tenant_id))[-1].payload
    assert payload["counts"]["cards"] == 1 and str(card.id) in payload["ids"]["cards"]


async def test_tenants_are_served_longest_waiting_first_so_the_cap_rotates(db):
    """A fixed cap over a fixed order would leave the same tenants beyond it every run."""
    served_before = await create_test_tenant(db, name="Served Before Corp")
    never_served = await create_test_tenant(db, name="Never Served Corp")
    await ops_digest.run_ops_digest(
        db, now=datetime.now(timezone.utc), sender=AsyncMock(), tenant_ids=[served_before.id]
    )

    tenants, _, last = await ops_digest.tenants_due(db)
    order = [t.id for t in tenants]
    assert order.index(never_served.id) < order.index(served_before.id)
    assert served_before.id in last and never_served.id not in last

    await ops_digest.run_ops_digest(
        db, now=datetime.now(timezone.utc), sender=AsyncMock(), tenant_ids=[never_served.id]
    )
    _, _, last = await ops_digest.tenants_due(db)
    assert never_served.id in last  # served once, it leaves the never-delivered front of the queue
    # Within one test transaction every audit row shares the transaction's now(), so the
    # relative order of two delivered tenants is not observable here; the key is covered above.


async def test_an_undelivered_digest_does_not_move_the_window(db, unknown_case):
    """A failed send keeps the incident in the next digest; a delivered one (or email
    deliberately off) advances the window as before."""
    tenant_id = unknown_case.actor.tenant_id
    await operation(db, unknown_case)
    first = datetime.now(timezone.utc)
    broken = AsyncMock(side_effect=RuntimeError("smtp down"))
    await ops_digest.run_ops_digest(db, now=first, sender=broken, tenant_ids=[tenant_id])
    rows = await _digest_rows(db, tenant_id)
    assert rows[-1].payload["delivery"] == "failed" and rows[-1].payload["counts"]["operations"] == 1

    working = AsyncMock()
    await ops_digest.run_ops_digest(db, now=first + timedelta(hours=1), sender=working, tenant_ids=[tenant_id])
    rows = await _digest_rows(db, tenant_id)
    assert rows[-1].payload["delivery"] == "sent"
    assert rows[-1].payload["counts"]["operations"] == 1  # still reported: nobody had seen it
    # The window floor is the failed digest's own start, not a fresh 24 hours from now.
    assert rows[-1].payload["since"] == (first - ops_digest.WINDOW).isoformat()

    await ops_digest.run_ops_digest(db, now=first + timedelta(hours=2), sender=working, tenant_ids=[tenant_id])
    rows = await _digest_rows(db, tenant_id)
    # Delivered once, so the window now starts at that delivered row. The incident's
    # updated_at equals the row's timestamp inside this one test transaction, so the
    # count cannot prove exclusion here; the boundary is what the fix changes.
    assert rows[-1].payload["since"] == rows[-2].timestamp.isoformat()


async def test_email_disabled_still_counts_as_delivered(db, unknown_case, monkeypatch):
    monkeypatch.setattr(settings, "OPS_DIGEST_EMAIL_ENABLED", False)
    tenant_id = unknown_case.actor.tenant_id
    await operation(db, unknown_case)
    first = datetime.now(timezone.utc)
    await ops_digest.run_ops_digest(db, now=first, sender=AsyncMock(), tenant_ids=[tenant_id])
    await ops_digest.run_ops_digest(db, now=first + timedelta(hours=1), sender=AsyncMock(), tenant_ids=[tenant_id])
    rows = await _digest_rows(db, tenant_id)
    assert rows[-2].payload["delivery"] == "disabled"
    assert rows[-1].payload["since"] == rows[-2].timestamp.isoformat()  # the audit row was the digest; the window moved


async def test_a_tenant_never_delivered_to_keeps_the_oldest_failed_window(db, unknown_case):
    """No delivered digest ever: the window floor is the first failed digest's own start, so the
    first digest a person receives covers the whole backlog instead of a fresh 24 hours."""
    tenant_id = unknown_case.actor.tenant_id
    await operation(db, unknown_case)
    first = datetime.now(timezone.utc)
    broken = AsyncMock(side_effect=RuntimeError("smtp down"))
    await ops_digest.run_ops_digest(db, now=first, sender=broken, tenant_ids=[tenant_id])
    await ops_digest.run_ops_digest(db, now=first + timedelta(hours=25), sender=broken, tenant_ids=[tenant_id])
    rows = await _digest_rows(db, tenant_id)
    assert [r.payload["delivery"] for r in rows[-2:]] == ["failed", "failed"]
    assert rows[-1].payload["since"] == rows[-2].payload["since"] == (first - ops_digest.WINDOW).isoformat()

    await ops_digest.run_ops_digest(db, now=first + timedelta(hours=26), sender=AsyncMock(), tenant_ids=[tenant_id])
    rows = await _digest_rows(db, tenant_id)
    assert rows[-1].payload["delivery"] == "sent"
    assert rows[-1].payload["since"] == (first - ops_digest.WINDOW).isoformat()  # the backlog, not 24h
