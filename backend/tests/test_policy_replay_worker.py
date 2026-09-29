import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.services.transaction_ops import policy_replay as service
from app.workers.tasks import transaction_ops as tasks


@pytest.fixture
def setup(monkeypatch):
    db = object()

    @asynccontextmanager
    async def session(**options):
        yield db

    monkeypatch.setattr(tasks, "worker_async_session", session)
    process = AsyncMock(side_effect=["pending", "finished"])
    publish = AsyncMock(return_value=True)
    failure = AsyncMock()
    monkeypatch.setattr(service, "process_batch", process)
    monkeypatch.setattr(service, "publish", publish)
    monkeypatch.setattr(service, "record_failure", failure)
    return db, process, publish, failure


def test_worker_completes_multiple_batches_without_republication(setup):
    db, process, publish, failure = setup
    tenant, replay = uuid4(), uuid4()
    assert tasks.transaction_policy_replay.run(str(tenant), str(replay)) == {"status": "finished"}
    assert process.await_count == 2
    process.assert_awaited_with(db, tenant, replay)
    publish.assert_not_awaited()
    failure.assert_not_awaited()


def test_worker_yields_and_republishes_at_time_bound(setup, monkeypatch):
    _, process, publish, failure = setup
    monkeypatch.setitem(sys.modules, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 1, 76])))
    tenant, replay = uuid4(), uuid4()
    assert tasks.transaction_policy_replay.run(str(tenant), str(replay)) == {"status": "pending"}
    assert process.await_count == 1
    publish.assert_awaited_once_with(tenant, replay)
    failure.assert_not_awaited()


def test_worker_records_sanitized_error_and_closes_attempt_on_retry(setup, monkeypatch):
    db, process, publish, failure = setup
    process.side_effect = service.state.StateError("policy_replay_contract_changed", 409)
    tenant, replay = uuid4(), uuid4()
    retry = Mock(side_effect=RuntimeError("retried"))
    monkeypatch.setattr(tasks.transaction_policy_replay, "retry", retry)
    with pytest.raises(RuntimeError, match="retried"):
        tasks.transaction_policy_replay.run(str(tenant), str(replay))
    failure.assert_awaited_once_with(db, tenant, replay, "policy_replay_contract_changed")
    assert str(retry.call_args.kwargs["exc"]) == "policy_replay_failed"
    close = Mock()
    task = tasks.PolicyReplayTask()
    monkeypatch.setattr(task, "on_failure", close)
    task.on_retry(RuntimeError("secret"), "attempt", (), {"tenant_id": str(tenant)}, None)
    assert str(close.call_args.args[0]) == "policy_replay_retry_scheduled"


async def test_publisher_passes_named_tenant_for_job_audit(monkeypatch):
    apply = Mock()
    monkeypatch.setattr(tasks.transaction_policy_replay, "apply_async", apply)
    tenant, replay = uuid4(), uuid4()
    assert await service.publish(tenant, replay)
    apply.assert_called_once_with(kwargs={"tenant_id": str(tenant), "replay_id": str(replay)})
