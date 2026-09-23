"""A cancelled local query must not strand the surrounding reconciliation slice."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from app.core import database
from app.services.transaction_ops.runner import run_investigation
from app.services.transaction_ops.state_service import StateError
from tests.conftest import _test_db_url
from tests.test_transaction_ops_runner import REF, State, missing_target, source_order


@pytest.mark.parametrize("pin", [False, True])
@pytest.mark.parametrize("stage", ["source", "target"])
@pytest.mark.parametrize("failure", ["deadline", "unexpected"])
async def test_cancelled_database_read_can_finalize_without_losing_its_cursor(monkeypatch, pin, stage, failure):
    monkeypatch.setattr(database, "_db_url", _test_db_url)
    state = State()
    async with database.worker_async_session(pin_connection=pin) as db:
        old_pid = await db.scalar(text("SELECT pg_backend_pid()"))
        await db.commit()
        state.run.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=0.15 if failure == "deadline" else 10)
        original_finish, original_settle = state.finish_run, state.settle_budget

        async def finish(*args, **kwargs):
            # Real state_service finalization likewise restores tenant scope
            # before reading the fenced run and committing its termination.
            await database.set_tenant_context(db, str(state.tenant))
            assert await db.scalar(text("SELECT current_setting('app.current_tenant_id', true)")) == str(state.tenant)
            assert await db.scalar(text("SELECT pg_backend_pid()")) != old_pid
            assert getattr(state, "held", 0) == (10 if stage == "target" else 0)
            await db.commit()
            return await original_finish(*args, **kwargs)

        async def settle(*args, **kwargs):
            # Settlement also uses the session, so it must fail while the
            # cancelled query's transaction is invalid. Its hold stays charged.
            await db.execute(text("SELECT 1"))
            return await original_settle(*args, **kwargs)

        async def cancelled_query(*args, **kwargs):
            if failure == "deadline":
                await db.execute(text("SELECT pg_sleep(5)"))
            else:
                try:
                    async with asyncio.timeout(0.03):
                        await db.execute(text("SELECT pg_sleep(5)"))
                except TimeoutError:
                    raise ValueError("private provider payload") from None
            pytest.fail("query should have been cancelled")

        state.finish_run, state.settle_budget = finish, settle
        result = await run_investigation(
            db,
            state.tenant,
            state.run_id,
            _state=state,
            _source_reader=cancelled_query if stage == "source" else AsyncMock(return_value=source_order()),
            _target_reader=cancelled_query if stage == "target" else AsyncMock(return_value=missing_target()),
            _order_mirror=AsyncMock(),
            _enabled=AsyncMock(return_value=True),
        )
        assert result["termination_reason"] == ("budget" if failure == "deadline" else "error")
        assert state.run.status == "finished"
        assert state.run.progress_json["pending_refs"] == [REF]
        assert not state.reports
        assert "private provider payload" not in repr(result) + repr(state.run.progress_json)


async def test_external_cancellation_is_not_converted_into_a_completed_slice():
    state, db = State(), AsyncMock()
    state.run.deadline_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    state.finish_run = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await run_investigation(
            db,
            state.tenant,
            state.run_id,
            _state=state,
            _source_reader=AsyncMock(side_effect=asyncio.CancelledError()),
            _enabled=AsyncMock(return_value=True),
        )
    state.finish_run.assert_not_awaited()
    db.rollback.assert_not_awaited()  # Session ownership handles external cancellation.


async def test_failed_read_finalization_still_obeys_the_owner_fence():
    state, db = State(), AsyncMock()
    state.run.deadline_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    state.finish_run = AsyncMock(side_effect=StateError("run_lease_lost"))
    with pytest.raises(StateError, match="run_lease_lost"):
        await run_investigation(
            db,
            state.tenant,
            state.run_id,
            _state=state,
            _source_reader=AsyncMock(side_effect=ValueError("private provider payload")),
            _enabled=AsyncMock(return_value=True),
        )
    db.rollback.assert_awaited_once()
    assert state.finish_run.call_args.kwargs["lease_token"] == state.token
    assert state.run.status == "running"
