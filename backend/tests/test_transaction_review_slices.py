from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.services.transaction_ops import period_review
from app.services.transaction_ops import state_service as state
from tests.test_transaction_period_review_api import ready


async def review(db, actor, monkeypatch):
    config = await ready(db, actor, monkeypatch)
    request = period_review.PeriodReview(evaluation_key=uuid4(), period="last_month")
    run = await period_review.create_review(db, actor.tenant_id, config.id, request, actor=actor)
    return config, run


async def finish(db, run, reason="done"):
    run.status = "finished"
    run.termination_reason = reason
    run.finished_at = datetime.now(timezone.utc)
    run.progress_json = {"scan_complete": True, "refund_scan_complete": True, "processed": 2, "matched": 2}
    await db.flush()


@pytest.mark.asyncio
async def test_month_starts_with_a_bounded_day_and_resumes_next_day_idempotently(db, admin_user, monkeypatch):
    actor = admin_user[0]
    config, run = await review(db, actor, monkeypatch)
    params = run.params_json
    assert datetime.fromisoformat(params["window_end"]) - datetime.fromisoformat(params["window_start"]) == timedelta(
        days=1
    )
    assert params["review"]["end"].startswith("2026-09-01T07:00:00")
    await finish(db, run)
    child = await period_review.continue_review(db, actor.tenant_id, run.id)
    assert child.params_json["window_start"] == params["window_end"]
    assert child.params_json["review"] == params["review"]
    assert child.progress_json == {} and child.initiated_by == actor.id
    assert (await period_review.continue_review(db, actor.tenant_id, run.id)).id == child.id
    summary = await period_review.review_status(db, actor.tenant_id, run.id)
    assert summary["status"] == "running" and summary["complete"] is False
    assert summary["completed_until"] == params["window_end"]


@pytest.mark.asyncio
async def test_failed_day_and_pause_cannot_skip_history_or_refresh_a_cycle_budget(db, admin_user, monkeypatch):
    from app.schemas.transaction_runs import ConfigControl

    actor = admin_user[0]
    config, run = await review(db, actor, monkeypatch)
    await finish(db, run, "budget")
    assert await period_review.continue_review(db, actor.tenant_id, run.id) is None
    summary = await period_review.review_status(db, actor.tenant_id, run.id)
    assert not summary["complete"] and summary["completed_until"] == run.params_json["review"]["start"]
    await state.control_config(
        db, actor.tenant_id, config.id, ConfigControl(enabled=False, schedule_enabled=False), actor=actor
    )
    assert await period_review.continue_review(db, actor.tenant_id, run.id) is None


@pytest.mark.asyncio
async def test_daily_slice_recovery_survives_worker_exit_before_next_queue(db, admin_user, monkeypatch):
    from app.services.transaction_ops.scheduler import _recovery_ids

    actor = admin_user[0]
    _, run = await review(db, actor, monkeypatch)
    await finish(db, run)
    assert run.id in await _recovery_ids(db, actor.tenant_id, datetime.now(timezone.utc))
    child = await period_review.continue_review(db, actor.tenant_id, run.id)
    eligible = await _recovery_ids(db, actor.tenant_id, datetime.now(timezone.utc))
    assert run.id not in eligible and child.id in eligible


@pytest.mark.asyncio
async def test_whole_month_closes_only_after_every_day_and_last_day_stops(db, admin_user, monkeypatch):
    actor = admin_user[0]
    _, root = await review(db, actor, monkeypatch)
    run = root
    for day in range(31):
        await finish(db, run)
        summary = await period_review.review_status(db, actor.tenant_id, root.id)
        assert summary["completed_slices"] == day + 1
        assert summary["complete"] is (day == 30)
        child = await period_review.continue_review(db, actor.tenant_id, run.id)
        if day == 30:
            assert child is None and summary["status"] == "complete"
            assert summary["completion_basis"] == "scan_coverage"
            assert summary["comparison_basis"] == "current_evidence_for_period_cohort"
            assert summary["source_freshness"] == "unverified"
            assert summary["financial_status"] == "not_certified"
        else:
            assert child is not None
            run = child


@pytest.mark.asyncio
async def test_incomplete_done_day_cannot_advance(db, admin_user, monkeypatch):
    actor = admin_user[0]
    _, run = await review(db, actor, monkeypatch)
    run.status = "finished"
    run.termination_reason = "done"
    run.finished_at = datetime.now(timezone.utc)
    run.progress_json = {"scan_complete": False, "refund_scan_complete": False}
    await db.flush()
    assert await period_review.continue_review(db, actor.tenant_id, run.id) is None


@pytest.mark.asyncio
async def test_other_tenant_cannot_read_or_advance_review(db, admin_user, tenant_b, monkeypatch):
    _, run = await review(db, admin_user[0], monkeypatch)
    for fn in (period_review.review_status, period_review.continue_review):
        with pytest.raises(state.StateError, match="not_found"):
            await fn(db, tenant_b.id, run.id)


@pytest.mark.asyncio
async def test_completed_worker_dispatches_the_next_durable_review_slice(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.transaction_ops import runner, scheduler
    from app.workers.tasks import transaction_ops as worker

    db = AsyncMock()
    tenant, run_id, child_id = uuid4(), uuid4(), uuid4()

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(worker, "worker_async_session", session)
    monkeypatch.setattr(worker, "set_tenant_context", AsyncMock())
    monkeypatch.setattr(runner, "run_investigation", AsyncMock(return_value={"termination_reason": "done"}))
    follow = AsyncMock(return_value=SimpleNamespace(id=child_id, status="pending"))
    monkeypatch.setattr(period_review, "continue_review", follow)
    dispatch = AsyncMock()
    monkeypatch.setattr(scheduler, "_dispatch", dispatch)
    result = await asyncio.to_thread(worker.transaction_ops_run.run, str(tenant), str(run_id))
    assert result["continuation_run_id"] == str(child_id)
    follow.assert_awaited_once_with(db, tenant, run_id)
    assert dispatch.call_args.args[:2] == (tenant, child_id)


@pytest.mark.asyncio
async def test_revoked_initiator_stops_automatic_review_and_recovery_retries(db, admin_user, monkeypatch):
    from sqlalchemy import update

    from app.models.user import User
    from app.services.transaction_ops.scheduler import _recovery_ids

    actor = admin_user[0]
    _, run = await review(db, actor, monkeypatch)
    await finish(db, run)
    await db.execute(update(User).where(User.id == actor.id).values(is_active=False))
    assert await period_review.continue_review(db, actor.tenant_id, run.id) is None
    assert run.id not in await _recovery_ids(db, actor.tenant_id, datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_coverage_prefers_final_continuation_over_query_order(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    span = {"id": str(uuid4()), "start": "2026-08-01T00:00:00Z", "end": "2026-09-01T00:00:00Z"}
    params = {"review": span, "window_start": span["start"], "window_end": "2026-08-02T00:00:00Z"}
    base = dict(config_id=uuid4(), params_json=params, status="finished")
    prior = SimpleNamespace(id=uuid4(), termination_reason="budget", progress_json={"continuation_part": 1}, **base)
    final = SimpleNamespace(
        id=uuid4(),
        termination_reason="done",
        progress_json={"continuation_part": 2, "scan_complete": True, "refund_scan_complete": True},
        **base,
    )
    monkeypatch.setattr(state, "get_run", AsyncMock(return_value=prior))
    db = AsyncMock()
    db.scalars.return_value = SimpleNamespace(all=lambda: [final, prior])
    summary = await period_review.review_status(db, uuid4(), prior.id)
    assert summary["completed_until"] == params["window_end"]
