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
from tests.test_transaction_review_slices import finish
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
