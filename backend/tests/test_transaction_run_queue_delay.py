"""Queue delay cannot consume execution time or reset spent investigation budgets."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.models.transaction_ops import TransactionRun
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import state_service as state
from tests.test_transaction_ops_state_db import seed_config


async def queued_run(db, actor, *, queued_at, origin="manual"):
    config = await seed_config(db, actor.tenant_id, actor, schedule_enabled=True, deadline_seconds=900)
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(origin=origin, evaluation_key="queued", order_references=["R123456789"]),
        actor=actor if origin != "schedule" else None,
        now=queued_at,
    )
    return config, run


@pytest.mark.parametrize("origin", ["manual", "chat", "schedule"])
@pytest.mark.parametrize("delay", [60, 1500])
async def test_execution_budget_starts_on_first_claim(db, admin_user, origin, delay):
    actor, _ = admin_user
    now = datetime.now(timezone.utc)
    _, run = await queued_run(db, actor, queued_at=now - timedelta(seconds=delay), origin=origin)
    token = await state.claim_run(db, actor.tenant_id, run.id, now=now)
    assert token is not None
    assert run.deadline_at == now + timedelta(seconds=900)
    assert await state.reserve_budget(db, actor.tenant_id, run.id, lease_token=token, api_calls=1, now=now)


async def test_reclaim_preserves_deadline_and_spend(db, admin_user):
    actor, _ = admin_user
    now = datetime.now(timezone.utc)
    _, run = await queued_run(db, actor, queued_at=now - timedelta(minutes=25))
    first = await state.claim_run(db, actor.tenant_id, run.id, now=now)
    assert first is not None
    deadline = run.deadline_at
    await state.reserve_budget(db, actor.tenant_id, run.id, lease_token=first, api_calls=5, now=now)
    second = await state.claim_run(db, actor.tenant_id, run.id, now=now + timedelta(seconds=181))
    assert second is not None and second != first
    assert run.deadline_at == deadline and run.api_calls_used == 5
    assert await state.claim_run(db, actor.tenant_id, run.id, now=deadline) is None
    assert run.status == "finished" and run.termination_reason == "budget"
    assert await state.claim_run(db, actor.tenant_id, run.id, now=deadline + timedelta(hours=1)) is None


async def test_unstarted_work_expires_after_a_day_in_queue(db, admin_user):
    actor, _ = admin_user
    now = datetime.now(timezone.utc)
    _, run = await queued_run(db, actor, queued_at=now - timedelta(days=1))
    assert await state.claim_run(db, actor.tenant_id, run.id, now=now) is None
    assert run.termination_reason == "budget" and run.api_calls_used == 0


async def test_first_claim_cannot_extend_a_continuation_past_its_cycle(db, admin_user):
    actor, _ = admin_user
    now = datetime.now(timezone.utc)
    _, run = await queued_run(db, actor, queued_at=now - timedelta(minutes=25))
    run.progress_json = {"continuation_started_at": (now - timedelta(hours=23, minutes=55)).isoformat()}
    await db.flush()
    assert await state.claim_run(db, actor.tenant_id, run.id, now=now) is not None
    assert run.deadline_at == now + timedelta(minutes=5)


async def test_queue_delay_does_not_extend_operation_recovery_deadlines(db, admin_user):
    actor, _ = admin_user
    now = datetime.now(timezone.utc)
    config = await seed_config(db, actor.tenant_id, actor, deadline_seconds=900)
    run = TransactionRun(
        tenant_id=actor.tenant_id,
        config_id=config.id,
        work_key="recovery-queue-test",
        origin="recovery",
        params_json={},
        config_snapshot={"deadline_seconds": 900},
        max_api_calls=32,
        max_orders=1,
        deadline_at=now - timedelta(minutes=10),
        progress_json={},
    )
    db.add(run)
    await db.flush()
    deadline = run.deadline_at
    assert await state.claim_run(db, actor.tenant_id, run.id, now=now) is None
    assert run.deadline_at == deadline


async def test_pausing_daily_schedule_prevents_queued_run_from_starting(db, admin_user):
    actor, _ = admin_user
    now = datetime.now(timezone.utc)
    config, run = await queued_run(db, actor, queued_at=now, origin="schedule")
    config.schedule_enabled = False
    await db.flush()
    assert await state.claim_run(db, actor.tenant_id, run.id, now=now) is None
    assert run.status == "finished" and run.termination_reason == "stall"


@pytest.mark.parametrize("mode", ["oversized", "already_started", "spent"])
async def test_database_rejects_deadline_extension_outside_first_claim_budget(db, admin_user, mode):
    actor, _ = admin_user
    now = datetime.now(timezone.utc)
    _, run = await queued_run(db, actor, queued_at=now - timedelta(seconds=600))
    if mode == "already_started":
        await state.claim_run(db, actor.tenant_id, run.id, now=now - timedelta(seconds=300))
    elif mode == "spent":
        run.api_calls_used = 1
        await db.flush()
    run_id = run.id
    with pytest.raises(DBAPIError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "UPDATE transaction_ops_runs SET status='running', lease_token=gen_random_uuid(), "
                    "deadline_at=:deadline WHERE id=:id AND tenant_id=:tenant"
                ),
                {
                    "deadline": now + timedelta(seconds=3600 if mode == "oversized" else 899),
                    "id": run_id,
                    "tenant": actor.tenant_id,
                },
            )


async def test_database_cannot_turn_started_work_back_into_an_unclaimed_run(db, admin_user):
    actor, _ = admin_user
    now = datetime.now(timezone.utc)
    _, run = await queued_run(db, actor, queued_at=now)
    await state.claim_run(db, actor.tenant_id, run.id, now=now)
    run_id = run.id
    with pytest.raises(DBAPIError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "UPDATE transaction_ops_runs SET status='pending', lease_token=NULL, lease_until=NULL "
                    "WHERE id=:id AND tenant_id=:tenant"
                ),
                {"id": run_id, "tenant": actor.tenant_id},
            )
