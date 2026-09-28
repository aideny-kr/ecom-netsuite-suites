"""Overlapping calendar requests share collection without sharing write authority."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.audit import AuditEvent
from app.models.transaction_ops import TransactionRun
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import period_review, runner
from app.services.transaction_ops import state_service as state
from tests.test_transaction_period_review_api import ready
from tests.test_transaction_review_slices import finish, review
from tests.test_transaction_run_atomic_updates import committed_run


def row(
    tenant,
    config,
    snapshot,
    *,
    origin="manual",
    created=None,
    start=None,
    end=None,
    basis="updated_at",
    references=None,
):
    now = datetime.now(timezone.utc)
    start = start or datetime(2026, 8, 1, 7, tzinfo=timezone.utc)
    end = end or start + timedelta(days=1)
    identifier = uuid4()
    params = {
        "origin": origin,
        "evaluation_key": str(identifier),
        "order_references": references or [],
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "window_basis": basis,
    }
    if origin != "schedule":
        params["review"] = {"id": str(identifier), "start": start.isoformat(), "end": end.isoformat()}
    if references:
        params.update(window_start=None, window_end=None, review=None)
    request = RunCreate(**params)
    params = request.model_dump(
        mode="json", exclude={"review", "window_basis"} if origin == "schedule" and basis == "updated_at" else set()
    )
    return TransactionRun(
        id=identifier,
        tenant_id=tenant,
        config_id=config,
        work_key=uuid4().hex,
        origin=origin,
        params_json=params,
        config_snapshot={**snapshot, "deadline_seconds": 900},
        max_api_calls=100,
        max_orders=100,
        deadline_at=now + timedelta(seconds=900),
        created_at=created or now,
        progress_json={},
        status="pending",
    )


async def pair(db, actor, monkeypatch, *, owner="schedule", **waiting_scope):
    config = await ready(db, actor, monkeypatch)
    config.schedule_enabled = True
    config.deadline_seconds = 900
    await db.flush()
    snapshot = state._config_snapshot(config)
    first = row(
        actor.tenant_id, config.id, snapshot, origin=owner, created=datetime.now(timezone.utc) - timedelta(seconds=3)
    )
    second = row(actor.tenant_id, config.id, snapshot, **waiting_scope)
    first.initiated_by = second.initiated_by = actor.id
    db.add_all([first, second])
    await db.flush()
    return config, first, second


@pytest.mark.parametrize("owner", ["schedule", "manual"])
async def test_waiting_run_spends_nothing_and_reuses_finished_owner(db, admin_user, monkeypatch, owner):
    actor = admin_user[0]
    _, first, second = await pair(db, actor, monkeypatch, owner=owner)
    assert await state.claim_run(db, actor.tenant_id, first.id)
    deadline = second.deadline_at
    provider = AsyncMock(side_effect=AssertionError("No repeated collection"))
    outcome = await runner.run_investigation(db, actor.tenant_id, second.id, _page_reader=provider)
    assert outcome["status"] == "pending"
    assert second.lease_token is None
    assert second.api_calls_used == second.api_calls_held == second.orders_used == 0
    assert second.deadline_at == deadline
    assert second.progress_json["collection_wait"]["run_id"] == str(first.id)
    await state.claim_run(db, actor.tenant_id, second.id)
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.tenant_id == actor.tenant_id,
                AuditEvent.action == "transaction_ops.run.collection_wait",
            )
        )
        == 1
    )
    await finish(db, first)
    result = await period_review._complete_saved_review(db, actor.tenant_id, second)
    assert result.termination_reason == "done"
    assert result.progress_json["reused_observation_run_ids"] == [str(first.id)]
    assert "collection_wait" not in result.progress_json
    assert result.api_calls_used == result.orders_used == 0
    provider.assert_not_awaited()


async def test_older_pending_owner_wins_even_when_newer_request_is_delivered_first(db, admin_user, monkeypatch):
    actor = admin_user[0]
    _, first, second = await pair(db, actor, monkeypatch)
    assert await state.claim_run(db, actor.tenant_id, second.id) is None
    assert await state.claim_run(db, actor.tenant_id, first.id)


@pytest.mark.parametrize("change", ["adjacent", "basis", "exact_order", "configuration"])
async def test_independent_collection_is_not_blocked(db, admin_user, monkeypatch, change):
    actor = admin_user[0]
    scope = {}
    if change == "adjacent":
        scope = {"start": datetime(2026, 8, 2, 7, tzinfo=timezone.utc)}
    elif change == "basis":
        scope = {"basis": "completed_at"}
    elif change == "exact_order":
        scope = {"references": ["R123456789"]}
    _, first, second = await pair(db, actor, monkeypatch, **scope)
    if change == "configuration":
        config = await ready(db, actor, monkeypatch)
        # A separate configuration cannot borrow the first one's lease/evidence.
        third = row(actor.tenant_id, config.id, state._config_snapshot(config))
        db.add(third)
        second = third
    await db.flush()
    assert await state.claim_run(db, actor.tenant_id, first.id)
    assert await state.claim_run(db, actor.tenant_id, second.id)


async def test_expired_lease_recovers_same_collector_without_competing_reads(db, admin_user, monkeypatch):
    actor = admin_user[0]
    _, first, second = await pair(db, actor, monkeypatch)
    assert await state.claim_run(db, actor.tenant_id, first.id)
    first.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.flush()
    assert await state.claim_run(db, actor.tenant_id, second.id) is None
    assert await state.claim_run(db, actor.tenant_id, first.id)


@pytest.mark.parametrize("reason", ["error", "stall"])
async def test_terminal_failure_does_not_leave_waiter_permanently_blocked(db, admin_user, monkeypatch, reason):
    actor = admin_user[0]
    _, first, second = await pair(db, actor, monkeypatch)
    await finish(db, first, reason)
    assert await state.claim_run(db, actor.tenant_id, second.id)


@pytest.mark.parametrize("owner", ["schedule", "manual"])
async def test_productive_budget_gap_keeps_owner_until_its_continuation(db, admin_user, monkeypatch, owner):
    from app.services.transaction_ops.continuation import continue_budget_run

    actor = admin_user[0]
    _, first, second = await pair(db, actor, monkeypatch, owner=owner)
    await finish(db, first, "budget")
    assert await state.claim_run(db, actor.tenant_id, second.id) is None
    child = await continue_budget_run(db, actor.tenant_id, first.id)
    assert child is not None, list(
        (await db.scalars(select(AuditEvent.payload).where(AuditEvent.resource_id == str(first.id)))).all()
    )
    assert await state.claim_run(db, actor.tenant_id, child.id)
    assert await state.claim_run(db, actor.tenant_id, second.id) is None


@pytest.mark.parametrize("cap", ["parts", "age", "no_progress"])
async def test_exhausted_collector_releases_waiter_without_resetting_its_budget(db, admin_user, monkeypatch, cap):
    actor = admin_user[0]
    _, first, second = await pair(db, actor, monkeypatch)
    first.status, first.termination_reason = "finished", "budget"
    first.finished_at = datetime.now(timezone.utc)
    first.progress_json = {
        "processed": 10,
        "continuation_part": 96 if cap == "parts" else 2,
        "continuation_baseline": {"processed": 10 if cap == "no_progress" else 0},
        "continuation_started_at": (
            datetime.now(timezone.utc) - timedelta(hours=25 if cap == "age" else 1)
        ).isoformat(),
    }
    await db.flush()
    before = dict(first.progress_json)
    assert await state.claim_run(db, actor.tenant_id, second.id)
    assert first.progress_json == before
    assert first.status == "finished"


async def test_other_tenant_cannot_claim_or_join_collection(db, admin_user, admin_user_b, monkeypatch):
    actor = admin_user[0]
    _, first, second = await pair(db, actor, monkeypatch)
    assert await state.claim_run(db, actor.tenant_id, first.id)
    with pytest.raises(state.StateError, match="not_found"):
        await state.claim_run(db, admin_user_b[0].tenant_id, second.id)
    assert second.status == "pending" and second.progress_json == {}


async def test_already_covered_review_finishes_while_daily_collector_runs(db, admin_user, monkeypatch):
    actor = admin_user[0]
    config, first, second = await pair(db, actor, monkeypatch)
    proof = row(actor.tenant_id, config.id, first.config_snapshot)
    db.add(proof)
    await db.flush()
    await finish(db, proof)
    assert await state.claim_run(db, actor.tenant_id, first.id)
    result = await period_review._complete_saved_review(db, actor.tenant_id, second)
    assert result.termination_reason == "done"
    assert result.api_calls_used == 0
    assert first.status == "running"


async def test_waiters_cannot_fill_recovery_scan_ahead_of_their_collector(db, admin_user, monkeypatch):
    from app.services.transaction_ops import scheduler

    actor = admin_user[0]
    _, older_waiter, newer_owner = await pair(db, actor, monkeypatch, owner="manual")
    older_waiter.progress_json = {
        "collection_wait": {"run_id": str(newer_owner.id), "reason": "overlapping_collection"}
    }
    newer_owner.progress_json = {
        "continuation_started_at": (older_waiter.created_at - timedelta(minutes=15)).isoformat(),
        "continuation_root_id": str(uuid4()),
        "continuation_part": 2,
    }
    await db.flush()
    monkeypatch.setattr(scheduler, "_SCAN_LIMIT", 1)
    recovered = await scheduler._recovery_ids(db, actor.tenant_id, datetime.now(timezone.utc))
    assert recovered[0] == newer_owner.id
    assert await state.claim_run(db, actor.tenant_id, newer_owner.id)


async def test_simultaneous_committed_claims_have_one_owner():
    # Separate real transactions/connections, not the savepoint test fixture.
    async with committed_run() as (factory, tenant, identifier, _):
        async with factory() as db:
            base = await state.get_run(db, tenant, identifier)
            first = row(
                tenant, base.config_id, base.config_snapshot, created=datetime.now(timezone.utc) - timedelta(seconds=2)
            )
            second = row(tenant, base.config_id, base.config_snapshot)
            db.add_all([first, second])
            await db.commit()

        async def claim(identifier):
            async with factory() as db:
                return await state.claim_run(db, tenant, identifier)

        results = await asyncio.gather(claim(second.id), claim(first.id))
        assert results[0] is None and results[1] is not None
        async with factory() as db:
            rows = (await db.scalars(select(TransactionRun).where(TransactionRun.id.in_([first.id, second.id])))).all()
            assert sorted(r.status for r in rows) == ["pending", "running"]
            assert sum(r.api_calls_used for r in rows) == 0


async def test_duplicate_claim_cannot_deadlock_run_then_config_proposal_lock(monkeypatch):
    # Reproduce propose()'s run->config locking while a duplicate claim takes
    # config->run, using committed fixtures and independent connections.
    async with committed_run() as (factory, tenant, identifier, _):
        async with factory() as db:
            base = await state.get_run(db, tenant, identifier)
            owner = row(tenant, base.config_id, base.config_snapshot)
            db.add(owner)
            await db.commit()
            assert await state.claim_run(db, tenant, owner.id)
        async with factory() as proposing, factory() as replay:
            await state.get_run(proposing, tenant, owner.id, lock=True)
            config_acquired = asyncio.Event()
            get_config = state.get_config

            async def observe_config(session, *args, **kwargs):
                result = await get_config(session, *args, **kwargs)
                if session is replay and kwargs.get("lock"):
                    config_acquired.set()
                return result

            monkeypatch.setattr(state, "get_config", observe_config)
            duplicate = asyncio.create_task(state.claim_run(replay, tenant, owner.id))
            try:
                async with asyncio.timeout(5):
                    await config_acquired.wait()
                    await get_config(proposing, tenant, owner.config_id, lock=True)
                    assert await duplicate is None
            finally:
                if not duplicate.done():
                    duplicate.cancel()
                await asyncio.gather(duplicate, return_exceptions=True)
                await proposing.rollback()


async def test_more_than_512_waiters_do_not_freeze_owner_or_lease_recovery(db, admin_user, monkeypatch):
    actor = admin_user[0]
    config, first, second = await pair(db, actor, monkeypatch)
    db.add_all([row(actor.tenant_id, config.id, first.config_snapshot) for _ in range(513)])
    await db.flush()
    assert await state.claim_run(db, actor.tenant_id, first.id)
    assert await state.claim_run(db, actor.tenant_id, second.id) is None
    first.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.flush()
    assert await state.claim_run(db, actor.tenant_id, first.id)


async def test_covered_retry_defers_if_another_worker_already_claimed_it(db, admin_user, monkeypatch):
    actor = admin_user[0]
    _, first, _ = await pair(db, actor, monkeypatch, owner="manual")
    assert await state.claim_run(db, actor.tenant_id, first.id)
    first.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.flush()
    assert await state.claim_run(db, actor.tenant_id, first.id, coverage_only=True) is None
    assert first.status == "running"


async def test_waiter_reads_missing_evidence_instead_of_forging_completed_coverage(db, admin_user, monkeypatch):
    from app.services.transaction_ops import metabase_reader

    actor = admin_user[0]
    _, first, second = await pair(db, actor, monkeypatch)
    assert await state.claim_run(db, actor.tenant_id, first.id)
    assert await state.claim_run(db, actor.tenant_id, second.id) is None
    # A done row without complete refund coverage cannot satisfy the review.
    first.status, first.termination_reason = "finished", "done"
    first.finished_at = datetime.now(timezone.utc)
    first.progress_json = {"scan_complete": True, "refund_scan_complete": False}
    await db.flush()
    provider = AsyncMock(side_effect=metabase_reader.ReplicaReadError("invalid_binding"))
    monkeypatch.setattr(metabase_reader, "read_order_page", provider)
    await runner.run_investigation(db, actor.tenant_id, second.id, _enabled=AsyncMock(return_value=True))
    provider.assert_awaited_once()
    assert "reused_observation_run_ids" not in second.progress_json
    assert "collection_wait" not in second.progress_json


async def test_waiting_review_cannot_prevent_collectors_next_daily_slice(db, admin_user, monkeypatch):
    actor = admin_user[0]
    config, owner = await review(db, actor, monkeypatch)
    waiter = row(actor.tenant_id, config.id, owner.config_snapshot)
    db.add(waiter)
    await db.flush()
    await finish(db, owner)
    child = await period_review.continue_review(db, actor.tenant_id, owner.id)
    assert child is not None
    assert child.params_json["window_start"] == owner.params_json["window_end"]
    assert await state.claim_run(db, actor.tenant_id, child.id)


async def test_waiting_does_not_extend_24_hour_queue_cap(db, admin_user, monkeypatch):
    actor = admin_user[0]
    config, first, _ = await pair(db, actor, monkeypatch)
    assert await state.claim_run(db, actor.tenant_id, first.id)
    aged = row(actor.tenant_id, config.id, first.config_snapshot)
    aged.deadline_at = datetime.now(timezone.utc) - timedelta(days=2) + timedelta(seconds=900)
    aged.progress_json = {"collection_wait": {"run_id": str(first.id), "reason": "overlapping_collection"}}
    db.add(aged)
    await db.flush()
    assert await state.claim_run(db, actor.tenant_id, aged.id) is None
    assert aged.status == "finished" and aged.termination_reason == "budget"
    assert aged.api_calls_used == aged.orders_used == 0
    assert first.status == "running"


async def test_disabled_pending_schedule_does_not_block_manual_review(db, admin_user, monkeypatch):
    actor = admin_user[0]
    config, _, second = await pair(db, actor, monkeypatch)
    config.schedule_enabled = False
    await db.flush()
    assert await state.claim_run(db, actor.tenant_id, second.id)
