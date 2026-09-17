from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from app.services.transaction_ops import action_scheduler as mod
from app.services.transaction_ops import state_service as state
from app.workers.base_task import InstrumentedTask
from app.workers.tasks import transaction_ops as workers
from tests import test_transaction_ops_dispatch as dispatch_fixtures
from tests import test_transaction_ops_executor as execution_fixtures
from tests import test_transaction_ops_recovery as recovery_fixtures
from tests.test_transaction_ops_dispatch import reserve
from tests.test_transaction_ops_executor import execute, operation
from tests.test_transaction_ops_recovery import mock_recovery

ready = dispatch_fixtures.ready

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
    for name in ("execute", "recover", "recover_credit", "complete_accounting", "collect_actions"):
        task = getattr(workers, f"transaction_ops_{name}")
        assert task.name == f"tasks.transaction_ops_{name}"
        assert isinstance(task, InstrumentedTask) and task.max_retries == 0
        # Short correction jobs have their own queue and worker; only the collector keeps
        # the control queue, and nothing here still rides the bulk `recon` queue.
        expected_queue = "recon-control" if name == "collect_actions" else "recon-actions"
        assert task.queue == expected_queue and task.time_limit <= 340
    entries = [
        e
        for e in workers.celery_app.conf.beat_schedule.values()
        if e["task"] == "tasks.transaction_ops_collect_actions"
    ]
    assert len(entries) == 1 and entries[0]["schedule"] == 60.0


async def test_a_receipted_attempt_is_recovered_only_after_its_deadline(db, ready, monkeypatch):
    """A receipt moves the row to committed_unverified while the sending process is still
    running its own readback; the recovery scan must not race it. Only after the attempt's
    deadline does the row become due for a read-only recovery."""
    actor, _, _, claim = ready
    assert await reserve(db, actor.tenant_id, claim)
    row = await state.record_receipt(
        db, actor.tenant_id, claim.operation_id, {"status": "accepted", "record_id": "63", "verified": False}
    )
    assert row.status == "committed_unverified"
    publish = AsyncMock()
    monkeypatch.setattr(mod, "_dispatch", publish)
    stats = await mod.collect_due_actions(db, datetime.now(timezone.utc))
    assert stats["recoveries"] == 0
    publish.assert_not_awaited()
    stats = await mod.collect_due_actions(db, row.deadline_at + timedelta(seconds=1))
    assert stats["recoveries"] == 1
    assert publish.call_args.args[:3] == (actor.tenant_id, "recover", row.id)


def test_the_publisher_sends_short_jobs_to_the_actions_queue_and_group_dispatch_to_recon():
    """An explicit queue kwarg beats task_routes, so the publisher must name the same queue
    the routes do: receipts, recoveries and single executions leave the bulk queue; a group
    dispatch (thirty children per slice) stays with the long work."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock, Mock
    from uuid import uuid4

    from app.workers.celery_app import RECON_ACTIONS_QUEUE, celery_app

    app = MagicMock()
    app.send_task = Mock()

    @contextmanager
    def connection(**_kwargs):
        yield "connection"

    app.connection_for_write = connection
    seen = {}
    for kind in ("execute", "recover", "credit_recover", "complete", "group"):
        mod.publish_action(uuid4(), kind, uuid4(), app=app)
        seen[kind] = app.send_task.call_args.kwargs["queue"]
    assert seen == {
        "execute": RECON_ACTIONS_QUEUE,
        "recover": RECON_ACTIONS_QUEUE,
        "credit_recover": RECON_ACTIONS_QUEUE,
        "complete": RECON_ACTIONS_QUEUE,
        "group": "recon",
    }
    for kind, queue in seen.items():
        route = celery_app.amqp.router.route({}, mod._TASKS[kind])
        # the routes agree with the explicit kwarg (the long group dispatch has no override)
        assert route.get("queue").name == queue if "queue" in route else queue == "recon", kind
        assert celery_app.tasks[mod._TASKS[kind]].queue == queue, kind
