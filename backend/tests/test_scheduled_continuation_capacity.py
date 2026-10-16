"""Daily catch-up earns bounded parts without forking an older checkpoint."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.audit import AuditEvent
from app.models.transaction_ops import TransactionRun
from app.schemas.transaction_runs import ConfigControl, RunCreate
from app.services.transaction_ops import continuation as cont
from app.services.transaction_ops import scheduler
from app.services.transaction_ops import state_service as state
from tests.conftest import enable_feature_flag
from tests.test_concurrent_source_pipeline import committed as committed_fixture
from tests.test_transaction_continuation import budget_run
from tests.test_transaction_ops_state_db import seed_config


def checkpoint(*, origin="schedule", part=16, progress=None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        origin=origin,
        status="finished",
        termination_reason="budget",
        created_at=now,
        finished_at=now,
        progress_json={
            "continuation_part": part,
            "processed": part,
            "scan_count": 0,
            "continuation_baseline": {"processed": part - 1, "scan_count": 0},
            **(progress or {}),
        },
    )


@pytest.mark.parametrize(
    "origin,part,allowed",
    [
        ("schedule", 16, True),
        ("schedule", 95, True),
        ("schedule", 96, False),
        ("manual", 16, False),
        ("chat", 16, False),
    ],
)
def test_scheduled_capacity_keeps_manual_and_chat_caps(origin, part, allowed):
    prior = checkpoint(origin=origin, part=part)
    if allowed:
        assert cont.next_metadata(prior, prior.finished_at)["continuation_part"] == part + 1
    else:
        with pytest.raises(ValueError, match="part_limit"):
            cont.next_metadata(prior, prior.finished_at)


def test_short_productive_parts_still_stop_at_fixed_scheduled_spend_bound():
    prior = checkpoint(part=1)
    now = prior.created_at
    for part in range(2, 97):
        metadata = cont.next_metadata(prior, now)
        assert metadata["continuation_part"] == part
        prior.id = uuid4()
        prior.progress_json.update(metadata, processed=part)
        now += timedelta(minutes=1)
    with pytest.raises(ValueError, match="part_limit"):
        cont.next_metadata(prior, now)
    assert now - prior.created_at < cont.MAX_CYCLE_AGE


@pytest.mark.parametrize("guard", ["age", "no_progress", "restart", "retry_limit"])
def test_later_scheduled_parts_preserve_existing_guards(guard):
    prior = checkpoint(part=40)
    p = prior.progress_json
    if guard == "age":
        p["continuation_started_at"] = (prior.created_at - timedelta(days=1)).isoformat()
    elif guard == "no_progress":
        p["continuation_baseline"]["processed"] = p["processed"]
    elif guard == "restart":
        p["restart_scan"] = True
    else:
        p.update(
            continuation_read_retry_count=3,
            read_stop_reason="retry_limit",
            read_stop_run_id=str(prior.id),
            last_read_failure={"code": "source_transport_failed", "retryable": True, "resolved": False},
        )
    expected = {"age": "cycle_expired", "restart": "no_progress", "retry_limit": "read_retry_limit"}.get(guard, guard)
    with pytest.raises(ValueError, match=expected):
        cont.next_metadata(prior, prior.finished_at)


async def legacy_stop(db, actor, monkeypatch, *, part=16, expired=False, no_progress=False):
    conf = await seed_config(db, actor.tenant_id, actor, interval_minutes=1440)
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
    await state.control_config(
        db, actor.tenant_id, conf.id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
    )
    now = datetime.now(timezone.utc).replace(hour=12, minute=30, second=0, microsecond=0)
    prior = await state.create_run(
        db,
        actor.tenant_id,
        conf.id,
        RunCreate(origin="schedule", evaluation_key=scheduler._bucket(now, 1440), order_references=["R100000001"]),
        now=now - timedelta(minutes=20),
    )
    prior.status, prior.termination_reason = "finished", "budget"
    prior.finished_at = now - timedelta(minutes=5)
    prior.progress_json = {
        "processed": 2985,
        "scan_count": 3277,
        "last_source_id": 16220400,
        "pending_refs": ["R100000001"],
        "continuation_part": part,
        "continuation_root_id": str(prior.id),
        "evidence_root_id": str(prior.id),
        "continuation_started_at": (now - timedelta(hours=25 if expired else 4)).isoformat(),
        "schedule_cycle_key": scheduler._bucket(now, 1440),
        "continuation_read_retry_count": 2,
        "continuation_baseline": {"processed": 2985 if no_progress else 2792, "scan_count": 3277},
    }
    await state._audit(db, actor.tenant_id, "run.continuation_blocked", prior, payload={"reason": "part_limit"})
    await db.flush()
    monkeypatch.setattr(scheduler, "_reserve_publication", Mock(return_value=True))
    monkeypatch.setattr(scheduler, "_refresh_sources", AsyncMock(return_value=0))
    monkeypatch.setattr(scheduler.celery_app, "send_task", Mock())
    return conf, prior, now


async def audit_count(db, actor, prior):
    return await db.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.tenant_id == actor.tenant_id,
            AuditEvent.resource_id == str(prior.id),
            AuditEvent.action == "transaction_ops.run.continuation_blocked",
        )
    )


async def test_real_scheduler_resumes_legacy_part_cap_once_without_resetting_evidence(db, admin_user, monkeypatch):
    actor = admin_user[0]
    conf, prior, now = await legacy_stop(db, actor, monkeypatch)
    saved = dict(prior.progress_json)
    assert prior.id not in await scheduler._recovery_ids(db, actor.tenant_id, now)
    assert conf.id in await scheduler._candidate_ids(db, actor.tenant_id, now)
    stats = await scheduler.collect_due_runs(db, now)
    assert stats["created"] == stats["dispatched"] == 1
    child, blocked = await cont.continuation_result(db, actor.tenant_id, prior.id)
    assert blocked["reason"] == "part_limit"  # Preserve historical audit.
    assert child.progress_json["continuation_part"] == 17
    assert child.progress_json["continuation_read_retry_count"] == 2
    for key in ("evidence_root_id", "last_source_id", "pending_refs", "continuation_started_at"):
        assert child.progress_json[key] == saved[key]
    assert child.max_api_calls == prior.max_api_calls
    assert child.max_orders == prior.max_orders
    assert prior.progress_json == saved
    assert (await scheduler.collect_due_runs(db, now + timedelta(seconds=1)))["created"] == 0
    assert (await cont.continue_budget_run(db, actor.tenant_id, prior.id, now=now)).id == child.id


@pytest.mark.parametrize("reason", ["paused", "permission_denied", "feature_unavailable", "read_retry_limit"])
@pytest.mark.parametrize("hard_first", [False, True])
async def test_legacy_cap_never_overrides_hard_audits_in_either_order(db, admin_user, monkeypatch, reason, hard_first):
    actor = admin_user[0]
    _, prior, now = await legacy_stop(db, actor, monkeypatch)
    await state._audit(db, actor.tenant_id, "run.continuation_blocked", prior, payload={"reason": reason})
    if hard_first:
        await state._audit(db, actor.tenant_id, "run.continuation_blocked", prior, payload={"reason": "part_limit"})
    before = await audit_count(db, actor, prior)
    for second in range(3):
        assert (await scheduler.collect_due_runs(db, now + timedelta(seconds=second)))["created"] == 0
    assert (await cont.continuation_result(db, actor.tenant_id, prior.id))[0] is None
    assert await audit_count(db, actor, prior) == before


@pytest.mark.parametrize("guard", ["expired", "new_cap", "no_progress", "paused", "feature"])
async def test_ineligible_legacy_cap_does_not_spin_or_append_repeated_audits(db, admin_user, monkeypatch, guard):
    actor = admin_user[0]
    conf, prior, now = await legacy_stop(
        db,
        actor,
        monkeypatch,
        part=96 if guard == "new_cap" else 16,
        expired=guard == "expired",
        no_progress=guard == "no_progress",
    )
    if guard == "paused":
        conf.enabled = conf.schedule_enabled = False
    elif guard == "feature":
        from sqlalchemy import update

        from app.models.feature_flag import TenantFeatureFlag

        await db.execute(
            update(TenantFeatureFlag)
            .where(TenantFeatureFlag.tenant_id == actor.tenant_id, TenantFeatureFlag.flag_key == "reconciliation")
            .values(enabled=False)
        )
    await db.flush()
    before = await audit_count(db, actor, prior)
    for second in range(3):
        assert (
            await cont.continue_budget_run(db, actor.tenant_id, prior.id, now=now + timedelta(seconds=second)) is None
        )
    assert await audit_count(db, actor, prior) == before + int(guard in {"paused", "feature"})


async def test_daily_rollover_preserves_checkpoint_and_old_parent_cannot_fork(db, admin_user, monkeypatch):
    actor = admin_user[0]
    conf, prior, now = await legacy_stop(db, actor, monkeypatch)
    saved = dict(prior.progress_json)
    tomorrow = now + timedelta(days=1)
    assert (await scheduler.collect_due_runs(db, tomorrow))["created"] == 1
    runs = await state.list_runs(db, actor.tenant_id, config_id=conf.id)
    newer = next(r for r in runs if r.id != prior.id)
    assert newer.progress_json["evidence_root_id"] == saved["evidence_root_id"]
    assert newer.progress_json["pending_refs"] == saved["pending_refs"]
    assert newer.progress_json["last_source_id"] == saved["last_source_id"]
    assert newer.progress_json["schedule_cycle_key"] == scheduler._bucket(tomorrow, 1440)
    assert "continuation_part" not in newer.progress_json
    assert "continuation_read_retry_count" not in newer.progress_json
    assert newer.progress_json["continuation_baseline"]["processed"] == saved["processed"]
    # Finish the successor, then replay an older productive unblocked parent.
    # Its age remains young here so only the supersession guard prevents a fork.
    newer.status, newer.termination_reason = "finished", "done"
    newer.finished_at = tomorrow
    await db.flush()
    assert await cont.continue_budget_run(db, actor.tenant_id, prior.id, now=now) is None
    assert (await cont.continuation_result(db, actor.tenant_id, prior.id))[0] is None


@pytest.fixture
async def committed_sessions(monkeypatch):
    async for seed in committed_fixture.__wrapped__(monkeypatch):
        yield seed


async def test_real_concurrent_resumes_create_one_scheduled_child(committed_sessions):
    db, actor, factory = committed_sessions
    prior, _ = await budget_run(db, actor, origin="schedule", progress={"continuation_part": 16})
    await db.commit()

    async def resume():
        async with factory() as session:
            child = await cont.continue_budget_run(session, actor.tenant_id, prior.id)
            return child.id

    ids = await asyncio.gather(resume(), resume())
    assert ids[0] == ids[1]
    assert (
        await db.scalar(
            select(func.count())
            .select_from(TransactionRun)
            .where(
                TransactionRun.tenant_id == actor.tenant_id,
                TransactionRun.progress_json["continuation_of"].astext == str(prior.id),
            )
        )
        == 1
    )


async def test_real_concurrent_error_retries_create_one_child(committed_sessions):
    db, actor, factory = committed_sessions
    prior, _ = await budget_run(db, actor, origin="schedule", reason="error", failure=TimeoutError())
    await db.commit()
    now = prior.finished_at + timedelta(minutes=5)

    async def resume():
        async with factory() as session:
            child = await cont.continue_budget_run(session, actor.tenant_id, prior.id, now=now)
            return child.id

    ids = await asyncio.gather(resume(), resume())
    assert ids[0] == ids[1]
    assert (
        await db.scalar(
            select(func.count())
            .select_from(TransactionRun)
            .where(
                TransactionRun.tenant_id == actor.tenant_id,
                TransactionRun.progress_json["continuation_of"].astext == str(prior.id),
            )
        )
        == 1
    )
