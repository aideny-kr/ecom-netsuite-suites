"""InstrumentedTask must record every run, even after the pooled sync connection died.

2026-10-26..28 on staging: the first instrumented task after a quiet spell (the 03:30
scheduled recon, the 04:30 envelope pass, the post-run agent pass) failed inside
``before_start``. The pooled sync connection had been closed by the server while it sat
idle, so writing the jobs row raised, the task body never ran, and no jobs row existed
to show the run was lost.

The same failure then mis-recorded an earlier run: ``_job_id`` lives on the task
singleton, so ``on_failure`` (which Celery calls when ``before_start`` raises) marked the
PREVIOUS run's jobs row failed. 25 finished transaction-ops scans read as failed that way.
A start that fails must instead leave the earlier row alone and record itself as failed.

These tests go through ``Task.apply`` (Celery's real tracer) against the local database.
They terminate a backend with ``pg_terminate_backend``, so they refuse to run anywhere
but a local Postgres.
"""

import time
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.job import Job
from app.workers import base_task
from app.workers.base_task import InstrumentedTask, sync_engine
from app.workers.celery_app import celery_app

pytestmark = pytest.mark.skipif(
    make_url(settings.DATABASE_URL_SYNC).host not in {"localhost", "127.0.0.1"},
    reason="terminates database backends; local Postgres only",
)


@celery_app.task(base=InstrumentedTask, name="tests.instrumented_task_probe")
def _probe(tenant_id=None):
    return {"ok": True}


@pytest.fixture
def tenant_id():
    """A tenant id no other test uses; its jobs and audit rows are removed afterwards."""
    _probe._job_id = None  # a fresh worker process
    tid = str(uuid.uuid4())
    yield tid
    with Session(sync_engine) as session:
        session.execute(delete(AuditEvent).where(AuditEvent.tenant_id == tid))
        session.execute(delete(Job).where(Job.tenant_id == tid))
        session.commit()


def _jobs(tenant_id: str) -> list[Job]:
    with Session(sync_engine) as session:
        return list(session.scalars(select(Job).where(Job.tenant_id == tenant_id).order_by(Job.started_at)))


def _kill_the_pooled_connection() -> None:
    """Leave exactly one connection in the pool, then have the server close it."""
    sync_engine.dispose()
    with sync_engine.connect() as conn:
        pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
    # Autocommit: pg_stat_activity is a snapshot that lasts the whole transaction.
    killer = create_engine(settings.DATABASE_URL_SYNC, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        with killer.connect() as conn:
            assert conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}).scalar_one()
            for _ in range(50):
                if not conn.execute(text("SELECT 1 FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}).first():
                    return
                time.sleep(0.1)
        raise AssertionError(f"backend {pid} was still alive after pg_terminate_backend")
    finally:
        killer.dispose()


def test_a_run_after_the_pooled_connection_died_still_runs_and_records_its_job(tenant_id):
    _kill_the_pooled_connection()

    result = _probe.apply(kwargs={"tenant_id": tenant_id})

    assert result.successful(), result.traceback
    jobs = _jobs(tenant_id)
    assert [(j.celery_task_id, j.status) for j in jobs] == [(result.id, "completed")]


def _fail_first_checkout(real_tenant_session):
    """The way a dead pooled connection fails; later checkouts get a fresh connection."""
    calls = {"n": 0}

    def tenant_session(tid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("SET LOCAL", {}, Exception("server closed the connection unexpectedly"))
        return real_tenant_session(tid)

    return tenant_session


def _fail_first_commit(real_tenant_session):
    """The jobs row is flushed, then the connection dies before the commit."""
    calls = {"n": 0}

    @contextmanager
    def tenant_session(tid):
        calls["n"] += 1
        with real_tenant_session(tid) as session:
            if calls["n"] == 1:

                def commit():
                    session.rollback()
                    raise OperationalError("COMMIT", {}, Exception("server closed the connection unexpectedly"))

                session.commit = commit
            yield session

    return tenant_session


@pytest.mark.parametrize("failure", [_fail_first_checkout, _fail_first_commit], ids=["checkout", "commit"])
def test_a_start_that_fails_is_recorded_without_rewriting_the_previous_run(tenant_id, monkeypatch, failure):
    first = _probe.apply(kwargs={"tenant_id": tenant_id})
    assert first.successful(), first.traceback

    monkeypatch.setattr(base_task, "tenant_session", failure(base_task.tenant_session))
    second = _probe.apply(kwargs={"tenant_id": tenant_id})
    assert second.failed()

    jobs = _jobs(tenant_id)
    assert [(j.celery_task_id, j.status) for j in jobs] == [(first.id, "completed"), (second.id, "failed")]
    assert jobs[0].error_message is None
    assert "server closed the connection unexpectedly" in jobs[1].error_message
