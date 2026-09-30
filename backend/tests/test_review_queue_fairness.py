"""Real leases, stop fencing, preserved budgets and recoverable queue fairness."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.audit import AuditEvent
from app.models.transaction_ops import TransactionFinding
from app.schemas.transaction_runs import ProgressUpdate
from app.services.transaction_ops import continuation, period_review, runner, scheduler
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.review_control import stop_review, stopped
from tests.test_cached_period_review import request, setup


async def test_checkpoint_yields_without_spend_or_deadline_reset_and_resumes(db, admin_user):
    actor = admin_user[0]
    _, _, _, run = await setup(db, actor)
    provider = AsyncMock(side_effect=AssertionError("No provider reads for saved evidence"))
    ticks = iter([0, 61])
    result = await runner.run_investigation(
        db,
        actor.tenant_id,
        run.id,
        _slice_seconds=60,
        _slice_clock=lambda: next(ticks),
        _source_reader=provider,
        _target_reader=provider,
        _page_reader=provider,
    )
    assert result == {"run_id": str(run.id), "status": "yielded", "generation": 1}
    await db.refresh(run)
    deadline = run.deadline_at
    assert run.status == "running" and run.lease_token is run.lease_until is None
    from app.services.transaction_ops.operational_status import run_snapshot

    assert run_snapshot(run, state._clock())["execution_state"] == "queued"
    assert run.api_calls_used == run.orders_used == 0
    assert run.progress_json["pending_refs"] == ["R123456780"]
    assert run.id in await scheduler._recovery_ids(db, actor.tenant_id, state._clock())
    result = await runner.run_investigation(
        db,
        actor.tenant_id,
        run.id,
        _source_reader=provider,
        _target_reader=provider,
        _page_reader=provider,
    )
    assert result["termination_reason"] == "done"
    assert run.deadline_at == deadline
    assert "worker_yielded_at" not in run.progress_json
    assert (
        await db.scalar(select(func.count()).select_from(TransactionFinding).where(TransactionFinding.run_id == run.id))
        == 1
    )
    provider.assert_not_awaited()


async def test_yield_fences_old_owner_and_does_not_release_unsettled_spend(db, admin_user):
    actor = admin_user[0]
    _, _, _, run = await setup(db, actor)
    token = await state.claim_run(db, actor.tenant_id, run.id)
    await state.reserve_budget(db, actor.tenant_id, run.id, lease_token=token, api_calls=2, hold=True)
    with pytest.raises(state.StateError, match="run_not_yieldable"):
        await state.yield_run(
            db, actor.tenant_id, run.id, ProgressUpdate(progress_json=run.progress_json), lease_token=token
        )
    await state.settle_budget(db, actor.tenant_id, run.id, lease_token=token, release=2, spent=1)
    await state.yield_run(
        db, actor.tenant_id, run.id, ProgressUpdate(progress_json=run.progress_json), lease_token=token
    )
    deadline = run.deadline_at
    new = await state.claim_run(db, actor.tenant_id, run.id)
    assert new != token and run.api_calls_used == 1 and run.deadline_at == deadline
    with pytest.raises(state.StateError, match="run_lease_lost"):
        await state.update_progress(db, actor.tenant_id, run.id, ProgressUpdate(progress_json={}), lease_token=token)


async def test_stopping_review_fences_owner_and_all_recovery_without_changing_daily_config(db, admin_user):
    actor = admin_user[0]
    _, config, old, run = await setup(db, actor)
    flags = (config.enabled, config.schedule_enabled)
    token = await state.claim_run(db, actor.tenant_id, run.id)
    await state.reserve_budget(db, actor.tenant_id, run.id, lease_token=token, api_calls=2, hold=True)
    before = await db.scalar(
        select(func.count()).select_from(TransactionFinding).where(TransactionFinding.tenant_id == actor.tenant_id)
    )
    first = await stop_review(db, actor.tenant_id, run.id, actor=actor)
    assert first == await stop_review(db, actor.tenant_id, run.id, actor=actor)
    assert (config.enabled, config.schedule_enabled) == flags
    assert (
        run.status == "finished"
        and run.termination_reason == "stall"
        and run.api_calls_used == 2
        and run.api_calls_held == 0
    )
    assert run.lease_token is None
    assert await state.claim_run(db, actor.tenant_id, run.id) is None
    assert await continuation.continue_budget_run(db, actor.tenant_id, run.id) is None
    assert await period_review.continue_review(db, actor.tenant_id, run.id) is None
    assert run.id not in await scheduler._recovery_ids(db, actor.tenant_id, state._clock())
    assert before == await db.scalar(
        select(func.count()).select_from(TransactionFinding).where(TransactionFinding.tenant_id == actor.tenant_id)
    )
    assert (await period_review.review_status(db, actor.tenant_id, run.id))["status"] == "stopped"
    assert not await stopped(db, actor.tenant_id, uuid4(), run.params_json["review"]["id"])
    assert not await stopped(db, uuid4(), config.id, run.params_json["review"]["id"])
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.tenant_id == actor.tenant_id, AuditEvent.action == "transaction_ops.review.stopped")
        )
        == 1
    )
    # A new explicit request gets a fresh span; the stopped span cannot resume.
    again = await period_review.create_review(db, actor.tenant_id, config.id, request(), actor=actor)
    assert again.params_json["review"]["id"] != run.params_json["review"]["id"]


async def test_stop_terminal_parent_prevents_next_day_without_mutating_history(db, admin_user):
    actor = admin_user[0]
    from datetime import date

    from pytest import MonkeyPatch

    from tests.test_transaction_period_review_api import ready

    with MonkeyPatch.context() as patch:
        config = await ready(db, actor, patch)
        run = await period_review.create_review(
            db,
            actor.tenant_id,
            config.id,
            period_review.PeriodReview(
                evaluation_key=uuid4(), period="custom", start_date=date(2026, 9, 1), end_date=date(2026, 9, 2)
            ),
            actor=actor,
        )
    token = await state.claim_run(db, actor.tenant_id, run.id)
    await state.update_progress(
        db,
        actor.tenant_id,
        run.id,
        ProgressUpdate(
            progress_json={
                "scan_complete": True,
                "refund_scan_complete": True,
                "destination_scan_complete": True,
                "dependency_scan_complete": True,
                "dependency_index_seed": {"complete": True},
            }
        ),
        lease_token=token,
    )
    await state.finish_run(db, actor.tenant_id, run.id, "done", lease_token=token)
    saved = dict(run.progress_json)
    await stop_review(db, actor.tenant_id, run.id, actor=actor)
    assert run.progress_json == saved and run.termination_reason == "done"
    assert await period_review.continue_review(db, actor.tenant_id, run.id) is None
    assert run.id not in await scheduler._recovery_ids(db, actor.tenant_id, state._clock())


async def test_stop_http_requires_permission_and_tenant_scope(client, app, db, admin_user, admin_user_b, readonly_user):
    from app.api.v1.transaction_ops import router
    from tests.conftest import enable_feature_flag

    app.include_router(router, prefix="/api/v1")
    actor, headers = admin_user
    _, _, _, run = await setup(db, actor)
    url = f"/api/v1/transaction-ops/runs/{run.id}/review/stop"
    assert (await client.post(url)).status_code == 401
    other, other_headers = admin_user_b
    await enable_feature_flag(db, other.tenant_id, "celigo")
    await enable_feature_flag(db, other.tenant_id, "reconciliation")
    assert (await client.post(url, headers=other_headers)).status_code in (403, 404)
    assert (await client.post(url, headers=readonly_user[1])).status_code == 403
    result = await client.post(url, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "stopped"
    assert (await client.post(url, headers=headers)).json() == result.json()


def test_slice_publications_have_distinct_bounded_dedup_keys():
    from types import SimpleNamespace
    from unittest.mock import Mock

    client = Mock()
    connection = SimpleNamespace(default_channel=SimpleNamespace(client=client))
    tenant, run = uuid4(), uuid4()
    scheduler._reserve_publication(connection, tenant, run)
    scheduler._reserve_publication(connection, tenant, run, 1)
    scheduler._reserve_publication(connection, tenant, run, 2)
    keys = [call.args[0] for call in client.set.call_args_list]
    assert len(set(keys)) == 3
    assert all(call.kwargs == {"nx": True, "ex": 300} for call in client.set.call_args_list)


def test_worker_republishes_checkpoint_before_returning(monkeypatch):
    from contextlib import asynccontextmanager

    from app.workers.tasks import transaction_ops

    db = object()

    @asynccontextmanager
    async def session(**kwargs):
        yield db

    tenant, run = uuid4(), uuid4()
    investigate = AsyncMock(return_value={"status": "yielded", "generation": 2})
    dispatch = AsyncMock()
    monkeypatch.setattr(transaction_ops, "worker_async_session", session)
    monkeypatch.setattr(transaction_ops, "set_tenant_context_session", AsyncMock())
    monkeypatch.setattr(runner, "run_investigation", investigate)
    monkeypatch.setattr(scheduler, "_dispatch", dispatch)
    result = transaction_ops.transaction_ops_run.run(str(tenant), str(run))
    assert result["status"] == "yielded"
    investigate.assert_awaited_once_with(db, tenant, run, _slice_seconds=60)
    assert dispatch.call_args.args[:2] == (tenant, run)
    assert dispatch.call_args.kwargs == {"generation": 2}


async def test_expired_yield_cannot_extend_budget(db, admin_user):
    from datetime import timedelta

    actor = admin_user[0]
    _, _, _, run = await setup(db, actor)
    token = await state.claim_run(db, actor.tenant_id, run.id)
    deadline = run.deadline_at
    await state.yield_run(
        db, actor.tenant_id, run.id, ProgressUpdate(progress_json=run.progress_json), lease_token=token
    )
    assert await state.claim_run(db, actor.tenant_id, run.id, now=deadline + timedelta(seconds=1)) is None
    assert run.status == "finished" and run.termination_reason == "budget" and run.deadline_at == deadline
