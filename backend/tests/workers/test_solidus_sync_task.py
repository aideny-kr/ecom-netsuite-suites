import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.ingestion.solidus_sync import SolidusImportError
from app.workers.tasks import solidus_sync as worker


def mocks(monkeypatch, summary):
    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(worker, "worker_async_session", session)
    sync = AsyncMock(return_value=summary)
    monkeypatch.setattr(worker, "sync_solidus_orders", sync)
    send = Mock(return_value=SimpleNamespace(id=str(uuid.uuid4())))
    monkeypatch.setattr(worker.celery_app, "send_task", send)
    return sync, send


def test_partial_refresh_continues_with_a_smaller_persisted_budget(monkeypatch):
    _, send = mocks(monkeypatch, {"termination_reason": "budget", "complete": False, "pages_read": 50})
    result = worker.solidus_sync(tenant_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()), pages_remaining=100)
    assert result["continuation_task_id"] == send.return_value.id
    assert send.call_args.kwargs["kwargs"]["pages_remaining"] == 50


@pytest.mark.parametrize("reason,complete", [("done", True), ("stall", False)])
def test_completed_or_stalled_import_does_not_loop(monkeypatch, reason, complete):
    _, send = mocks(monkeypatch, {"termination_reason": reason, "complete": complete, "pages_read": 1})
    worker.solidus_sync(tenant_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()))
    send.assert_not_called()


def test_exhausted_budget_stays_explicitly_partial(monkeypatch):
    _, send = mocks(monkeypatch, {"termination_reason": "budget", "complete": False, "pages_read": 1})
    result = worker.solidus_sync(tenant_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()), pages_remaining=1)
    assert result["complete"] is False
    assert result["reason"] == "refresh_budget_exhausted"
    send.assert_not_called()


def test_source_failure_propagates_to_instrumented_failed_job(monkeypatch):
    sync, send = mocks(monkeypatch, {})
    sync.side_effect = SolidusImportError("source_rate_limited")
    with pytest.raises(SolidusImportError, match="source_rate_limited"):
        worker.solidus_sync(tenant_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()))
    send.assert_not_called()
