from datetime import datetime, timezone
from unittest.mock import AsyncMock

from app.services.transaction_ops import action_scheduler as mod
from app.workers.base_task import InstrumentedTask
from app.workers.tasks import transaction_ops as workers
from tests import test_transaction_ops_executor as execution_fixtures
from tests import test_transaction_ops_recovery as recovery_fixtures
from tests.test_transaction_ops_executor import execute, operation
from tests.test_transaction_ops_recovery import mock_recovery

execution_case = execution_fixtures.execution_case
unknown_case = recovery_fixtures.unknown_case


async def test_approved_work_is_queued_even_when_periodic_scans_are_disabled(db, execution_case, monkeypatch):
    publish = AsyncMock()
    monkeypatch.setattr(mod, "_dispatch", publish)
    stats = await mod.collect_due_actions(db, datetime.now(timezone.utc))
    assert stats["executions"] == 1
    publish.assert_awaited_once()
    assert publish.call_args.args[:3] == (execution_case.actor.tenant_id, "execute", execution_case.proposal.id)
    execution_case.case.dispatch.assert_not_awaited()


async def test_completed_work_is_not_published_again(db, execution_case, monkeypatch):
    await execute(db, execution_case)
    publish = AsyncMock()
    monkeypatch.setattr(mod, "_dispatch", publish)
    await mod.collect_due_actions(db, datetime.now(timezone.utc))
    publish.assert_not_awaited()


async def test_unknown_is_queued_for_reads_once_and_completed_recovery_does_not_reset(db, unknown_case, monkeypatch):
    row = await operation(db, unknown_case)
    publish = AsyncMock()
    monkeypatch.setattr(mod, "_dispatch", publish)
    stats = await mod.collect_due_actions(db, datetime.now(timezone.utc))
    assert stats["recoveries"] == 1 and stats["executions"] == 0
    assert publish.call_args.args[:3] == (unknown_case.actor.tenant_id, "recover", row.id)
    from app.services.transaction_ops.recovery import recover_operation

    mock_recovery(monkeypatch, unknown_case, unchanged=True)
    await recover_operation(db, unknown_case.actor.tenant_id, row.id)
    publish.reset_mock()
    await mod.collect_due_actions(db, datetime.now(timezone.utc))
    publish.assert_not_awaited()


def test_action_workers_are_bounded_without_broker_retries_and_have_a_minute_collector():
    for name in ("execute", "recover", "recover_credit", "collect_actions"):
        task = getattr(workers, f"transaction_ops_{name}")
        assert task.name == f"tasks.transaction_ops_{name}"
        assert isinstance(task, InstrumentedTask) and task.max_retries == 0
        assert task.queue == "recon" and task.time_limit <= 340
    entries = [
        e
        for e in workers.celery_app.conf.beat_schedule.values()
        if e["task"] == "tasks.transaction_ops_collect_actions"
    ]
    assert len(entries) == 1 and entries[0]["schedule"] == 60.0
