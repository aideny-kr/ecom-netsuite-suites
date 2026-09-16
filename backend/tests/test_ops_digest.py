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
from tests.conftest import create_test_tenant
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
    return list(
        await db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tenant_id, AuditEvent.action == "ops.digest"))
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
