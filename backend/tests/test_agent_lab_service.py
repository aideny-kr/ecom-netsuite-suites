import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.agent_lab_run import AgentLabRun


@pytest.mark.asyncio
async def test_start_run_inserts_row_and_dispatches_celery(monkeypatch):
    from app.services.agent_lab import service

    db = AsyncMock()
    user = MagicMock(id=uuid.uuid4())
    tenant_id = uuid.UUID("ce3dfaad-626f-4992-84e9-500c8291ca0a")
    apply_async_mock = MagicMock(return_value=MagicMock(id="task-id"))
    monkeypatch.setattr(
        "app.services.agent_lab.service.agent_lab_run_task",
        MagicMock(apply_async=apply_async_mock),
    )

    run = await service.start_run(
        db=db, user=user, tenant_id=tenant_id,
        kind="benchmark", mode="all",
    )

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, AgentLabRun)
    assert added.tenant_id == tenant_id
    assert added.kind == "benchmark"
    assert added.mode == "all"
    assert added.status == "running"
    assert added.total_cases == 18  # benchmark: hardcoded
    assert added.triggered_by_user_id == user.id
    apply_async_mock.assert_called_once()


@pytest.mark.asyncio
async def test_start_run_raises_409_on_integrity_error(monkeypatch):
    from app.services.agent_lab import service
    from app.services.agent_lab.service import ConcurrentRunError

    db = AsyncMock()
    db.flush = AsyncMock(side_effect=IntegrityError("x", "y", Exception()))
    monkeypatch.setattr(
        "app.services.agent_lab.service.agent_lab_run_task",
        MagicMock(),
    )

    with pytest.raises(ConcurrentRunError):
        await service.start_run(
            db=db, user=MagicMock(id=uuid.uuid4()),
            tenant_id=uuid.uuid4(), kind="benchmark", mode="all",
        )


def test_finalize_run_sync_updates_row():
    from app.services.agent_lab import service

    db = MagicMock()
    run_id = uuid.uuid4()

    service.finalize_run_sync(
        db=db, run_id=run_id,
        status="completed", cost_usd_actual=5.84,
        error_message=None,
    )

    db.query.assert_called_once_with(AgentLabRun)
    update_call = db.query.return_value.filter_by.return_value.update
    update_call.assert_called_once()
    update_args = update_call.call_args[0][0]
    assert update_args["status"] == "completed"
    assert update_args["cost_usd_actual"] == 5.84
    assert "finished_at" in update_args
    db.commit.assert_called_once()


def test_finalize_run_sync_writes_error_message_on_failed():
    from app.services.agent_lab import service

    db = MagicMock()
    service.finalize_run_sync(
        db=db, run_id=uuid.uuid4(),
        status="failed", cost_usd_actual=0.0,
        error_message="boom",
    )
    update_args = db.query.return_value.filter_by.return_value.update.call_args[0][0]
    assert update_args["status"] == "failed"
    assert update_args["error_message"] == "boom"


def test_cancel_run_sets_redis_key_with_ttl():
    from app.services.agent_lab import service

    redis_mock = MagicMock()
    run_id = uuid.uuid4()
    result = service.cancel_run(redis_mock, run_id)
    redis_mock.set.assert_called_once_with(
        f"agent_lab_run:{run_id}:cancel", "1", ex=300
    )
    assert result is True


@pytest.mark.asyncio
async def test_list_patterns_returns_tenant_scoped_sorted():
    """Patterns filtered by tenant_id, sorted last_used_at DESC NULLS LAST."""
    from app.services.agent_lab import service

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result_mock)
    tenant_id = uuid.uuid4()

    await service.list_patterns(db, tenant_id)

    db.execute.assert_called_once()
    executed_stmt = db.execute.call_args[0][0]
    compiled = str(executed_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "tenant_id" in compiled.lower()
    assert "last_used_at desc" in compiled.lower()
