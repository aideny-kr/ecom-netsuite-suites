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

These tests go through Celery's real tracer against the local database: ``Task.apply``
(eager), and a worker tracer (``eager=False``) for the path that only a worker takes.
They terminate a backend with ``pg_terminate_backend``, so they refuse to run anywhere
but a local Postgres.
"""

import time
import uuid
from contextlib import contextmanager

import pytest
from celery.app.trace import build_tracer
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


_DEAD = "server closed the connection unexpectedly"


def _first_start_fails(real_tenant_session, at: str):
    """before_start's checkout fails at ``at`` the way a dying connection does. Later
    checkouts get a fresh connection, as they do once the pool has replaced it.

    checkout: the first statement raises. commit: the row is flushed, then the commit
    raises. reply-lost: the commit lands on the server, but the client still sees an error.
    """
    calls = {"n": 0}

    @contextmanager
    def tenant_session(tid):
        calls["n"] += 1
        if calls["n"] == 1 and at == "checkout":
            raise OperationalError("SET LOCAL", {}, Exception(_DEAD))
        with real_tenant_session(tid) as session:
            if calls["n"] == 1:
                real_commit = session.commit

                def commit():
                    real_commit() if at == "reply-lost" else session.rollback()
                    raise OperationalError("COMMIT", {}, Exception(_DEAD))

                session.commit = commit
            yield session

    return tenant_session


@pytest.mark.parametrize("retry", [False, True], ids=["next-run", "retry"])
@pytest.mark.parametrize("at", ["checkout", "commit", "reply-lost"])
def test_a_start_that_fails_is_recorded_once_without_rewriting_the_previous_run(tenant_id, monkeypatch, at, retry):
    """A Celery retry reuses the task id, so the earlier run's row must not be found by it."""
    first = _probe.apply(kwargs={"tenant_id": tenant_id})
    assert first.successful(), first.traceback

    monkeypatch.setattr(base_task, "tenant_session", _first_start_fails(base_task.tenant_session, at))
    second = _probe.apply(kwargs={"tenant_id": tenant_id}, task_id=first.id if retry else None)
    assert second.failed()

    jobs = _jobs(tenant_id)
    assert [(j.celery_task_id, j.status) for j in jobs] == [(first.id, "completed"), (second.id, "failed")]
    assert jobs[0].error_message is None
    assert _DEAD in jobs[1].error_message


def test_a_start_failure_that_cannot_be_recorded_still_reports_the_tasks_own_error(tenant_id, monkeypatch):
    def database_down(_tid):
        raise OperationalError("SET LOCAL", {}, Exception(_DEAD))

    monkeypatch.setattr(base_task, "tenant_session", database_down)
    result = _probe.apply(kwargs={"tenant_id": tenant_id})

    assert result.failed()
    assert isinstance(result.result, OperationalError) and _DEAD in str(result.result)
    monkeypatch.undo()
    assert _jobs(tenant_id) == []


def _run_in_worker(tenant_id) -> str:
    """One run through the worker's tracer. Unlike eager apply, its outermost except reaches
    on_failure, after popping the run's request."""
    task_id = str(uuid.uuid4())
    tracer = build_tracer(_probe.name, _probe, eager=False, app=celery_app)
    tracer(task_id, [], {"tenant_id": tenant_id}, {"id": task_id, "task": _probe.name, "delivery_info": {}})
    return task_id


@pytest.mark.parametrize(
    ("fails", "recorded"),
    [
        ("started-write", None),  # before before_start: this run has nothing to record
        ("on-success-commit", _DEAD),  # after it: this run's row must not stay running
        ("success-write", "Connection reset by peer"),
    ],
)
def test_a_failure_on_celerys_outer_path_is_recorded_on_its_own_run_only(tenant_id, monkeypatch, fails, recorded):
    """Celery's outermost except handles a raise outside the task body: the STARTED write
    (task_track_started) before before_start, or on_success and the SUCCESS write after it.
    It calls on_failure after popping the run's request."""
    backend = _probe.backend
    monkeypatch.setattr(backend, "store_result", lambda *a, **kw: None)
    monkeypatch.setattr(backend, "process_cleanup", lambda: None)
    first = _run_in_worker(tenant_id)

    if fails == "on-success-commit":
        calls = {"n": 0}
        real_tenant_session = base_task.tenant_session

        @contextmanager
        def second_checkout_dies(tid):
            calls["n"] += 1
            with real_tenant_session(tid) as session:
                if calls["n"] == 2:  # on_success's; before_start's was the first

                    def commit():
                        session.rollback()
                        raise OperationalError("COMMIT", {}, Exception(_DEAD))

                    session.commit = commit
                yield session

        monkeypatch.setattr(base_task, "tenant_session", second_checkout_dies)
    else:
        state = "STARTED" if fails == "started-write" else "SUCCESS"

        def store_result(task_id, result, written_state, *a, **kw):
            if written_state == state:
                raise ConnectionError("Error 104 while writing to socket. Connection reset by peer.")

        monkeypatch.setattr(backend, "store_result", store_result)
    second = _run_in_worker(tenant_id)

    jobs = {j.celery_task_id: j for j in _jobs(tenant_id)}
    assert (jobs[first].status, jobs[first].error_message) == ("completed", None)
    if recorded is None:
        assert second not in jobs
    else:
        assert jobs[second].status == "failed" and recorded in jobs[second].error_message
