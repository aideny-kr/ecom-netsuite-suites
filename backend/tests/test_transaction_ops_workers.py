"""Worker adapters call the same durable investigation/scheduler services."""

import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.workers.base_task import InstrumentedTask
from app.workers.tasks import transaction_ops as mod


def test_tasks_are_registered_and_collector_ticks_every_minute():
    app = mod.celery_app
    assert "app.workers.tasks.transaction_ops" in app.conf.include
    assert mod.transaction_ops_run.name == "tasks.transaction_ops_run"
    assert mod.transaction_ops_run.queue == "recon"
    assert isinstance(mod.transaction_ops_run, InstrumentedTask)
    assert mod.transaction_ops_run.max_retries == 0
    entries = [
        entry for entry in app.conf.beat_schedule.values() if entry["task"] == "tasks.transaction_ops_collect_due"
    ]
    assert len(entries) == 1 and entries[0]["schedule"] == 60.0


def test_run_uses_worker_session_and_shared_runner(monkeypatch):
    db = object()
    tenant_id, run_id = uuid.uuid4(), uuid.uuid4()

    @asynccontextmanager
    async def session():
        yield db

    runner = AsyncMock(return_value={"status": "finished", "termination_reason": "done"})
    monkeypatch.setattr(mod, "worker_async_session", session)
    monkeypatch.setattr(mod, "set_tenant_context", AsyncMock())
    monkeypatch.setitem(sys.modules, "app.services.transaction_ops.runner", SimpleNamespace(run_investigation=runner))
    result = mod.transaction_ops_run.run(str(tenant_id), str(run_id))
    runner.assert_awaited_once_with(db, tenant_id, run_id)
    assert result["termination_reason"] == "done"


def test_collector_passes_aware_utc_time_to_scheduler(monkeypatch):
    db = object()

    @asynccontextmanager
    async def session():
        yield db

    collect = AsyncMock(return_value={"dispatched": 0, "termination_reason": "done"})
    monkeypatch.setattr(mod, "worker_async_session", session)
    monkeypatch.setitem(
        sys.modules, "app.services.transaction_ops.scheduler", SimpleNamespace(collect_due_runs=collect)
    )
    result = mod.transaction_ops_collect_due.run()
    assert result["dispatched"] == 0
    args = collect.call_args.args
    assert args[0] is db and isinstance(args[1], datetime) and args[1].utcoffset().total_seconds() == 0


def test_budget_worker_publishes_only_the_durable_continuation(monkeypatch):
    db = object()
    tenant, parent, child = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    @asynccontextmanager
    async def session():
        yield db

    runner = AsyncMock(return_value={"status": "finished", "termination_reason": "budget"})
    resume = AsyncMock(return_value=SimpleNamespace(id=child, status="pending"))
    dispatch = AsyncMock()
    monkeypatch.setattr(mod, "worker_async_session", session)
    monkeypatch.setattr(mod, "set_tenant_context", AsyncMock())
    monkeypatch.setitem(sys.modules, "app.services.transaction_ops.runner", SimpleNamespace(run_investigation=runner))
    monkeypatch.setitem(sys.modules, "app.services.transaction_ops.continuation", SimpleNamespace(continue_budget_run=resume))
    monkeypatch.setitem(sys.modules, "app.services.transaction_ops.scheduler", SimpleNamespace(_dispatch=dispatch))
    result = mod.transaction_ops_run.run(str(tenant), str(parent))
    resume.assert_awaited_once_with(db, tenant, parent)
    assert dispatch.call_args.args[:2] == (tenant, child)
    assert result["continuation_run_id"] == str(child)


@pytest.mark.parametrize("task", ["run", "scheduler"])
def test_worker_failure_text_never_contains_upstream_details(monkeypatch, task):
    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(mod, "worker_async_session", session)
    monkeypatch.setattr(mod, "set_tenant_context", AsyncMock())
    failing = AsyncMock(side_effect=RuntimeError("secret connection URL"))
    if task == "run":
        monkeypatch.setitem(
            sys.modules, "app.services.transaction_ops.runner", SimpleNamespace(run_investigation=failing)
        )

        def call():
            return mod.transaction_ops_run.run(str(uuid.uuid4()), str(uuid.uuid4()))

        code = "transaction_investigation_failed"
    else:
        monkeypatch.setitem(
            sys.modules, "app.services.transaction_ops.scheduler", SimpleNamespace(collect_due_runs=failing)
        )
        call = mod.transaction_ops_collect_due.run
        code = "transaction_scheduler_failed"
    with pytest.raises(RuntimeError, match=f"^{code}$") as err:
        call()
    assert err.value.__suppress_context__ is True
