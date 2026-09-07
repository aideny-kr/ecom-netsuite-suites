"""Scheduling creates durable read jobs and recovers lost publications."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock
from uuid import uuid4

import pytest

from app.services.transaction_ops import scheduler as mod

NOW = datetime(2026, 9, 4, 18, 32, tzinfo=timezone.utc)
TENANT = uuid4()


def config(**changes):
    return SimpleNamespace(
        id=uuid4(), tenant_id=TENANT, enabled=True, schedule_enabled=True, interval_minutes=5, **changes
    )


def previous(reason="done", **changes):
    values = dict(
        id=uuid4(),
        status="finished",
        origin="schedule",
        termination_reason=reason,
        created_at=NOW - timedelta(minutes=6),
        params_json={
            "origin": "schedule",
            "evaluation_key": "schedule:old",
            "order_references": [],
            "window_start": (NOW - timedelta(minutes=11)).isoformat(),
            "window_end": (NOW - timedelta(minutes=6)).isoformat(),
        },
    )
    return SimpleNamespace(**(values | changes))


@pytest.fixture
def dependencies(monkeypatch):
    state = SimpleNamespace(get_config=AsyncMock(), create_run=AsyncMock())

    def request(**values):
        return SimpleNamespace(**values)

    monkeypatch.setattr(mod, "_dependencies", lambda: (state, request, None, None))
    monkeypatch.setattr(mod, "set_tenant_context", AsyncMock())
    monkeypatch.setattr(mod.feature_flag_service, "list_tenants_with_flags", AsyncMock(return_value=[TENANT]))
    monkeypatch.setattr(mod, "_recovery_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(mod, "_candidate_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(mod, "_refresh_sources", AsyncMock(return_value=0))
    monkeypatch.setattr(mod, "_schedule_history", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(mod.celery_app, "send_task", Mock())
    return state


def test_clock_bucket_is_utc_and_stable():
    assert mod._bucket(NOW, 5) == "schedule:2026-09-04T18:30:00+00:00"
    assert mod._bucket(NOW + timedelta(minutes=1), 5) == mod._bucket(NOW, 5)
    assert mod._bucket(NOW.astimezone(timezone(timedelta(hours=-7))), 5) == mod._bucket(NOW, 5)


async def test_naive_clock_fails_before_any_query(dependencies):
    with pytest.raises(ValueError, match="aware"):
        await mod.collect_due_runs(AsyncMock(), NOW.replace(tzinfo=None))
    mod.feature_flag_service.list_tenants_with_flags.assert_not_awaited()


async def test_first_schedule_commits_before_publication_and_needs_both_flags(dependencies):
    db, conf = AsyncMock(), config()
    dependencies.get_config.return_value = conf
    mod._candidate_ids.return_value = [conf.id]
    durable = SimpleNamespace(id=uuid4(), status="pending")
    committed = []

    async def create(*args, **kwargs):
        committed.append(True)
        return durable

    dependencies.create_run.side_effect = create
    mod.celery_app.send_task.side_effect = lambda *args, **kwargs: committed.append("sent")
    stats = await mod.collect_due_runs(db, NOW)
    mod.feature_flag_service.list_tenants_with_flags.assert_awaited_once_with(db, ("celigo", "reconciliation"))
    dependencies.get_config.assert_awaited_once_with(db, TENANT, conf.id, lock=True)
    args, kwargs = dependencies.create_run.call_args
    assert args[:3] == (db, TENANT, conf.id)
    assert args[3].origin == "schedule" and args[3].evaluation_key == mod._bucket(NOW, 5)
    assert args[3].window_start == NOW - timedelta(minutes=5) and args[3].window_end == NOW
    assert kwargs == {"actor": None, "now": NOW, "resume_from_run_id": None}
    assert committed == [True, "sent"]
    mod.celery_app.send_task.assert_called_once_with(
        "tasks.transaction_ops_run",
        kwargs={"tenant_id": str(TENANT), "run_id": str(durable.id)},
        queue="recon",
        retry=False,
        retry_policy={"max_retries": 0},
        connection=ANY,
        ignore_result=True,
    )
    assert stats["created"] == stats["dispatched"] == 1


@pytest.mark.parametrize("reason", ["budget", "stall", "error"])
async def test_incomplete_schedule_retries_exact_scope_next_interval(dependencies, reason):
    conf, prior = config(), previous(reason)
    dependencies.get_config.return_value = conf
    dependencies.create_run.return_value = SimpleNamespace(id=uuid4(), status="pending")
    mod._candidate_ids.return_value = [conf.id]
    mod._schedule_history.return_value = (False, prior)
    await mod.collect_due_runs(AsyncMock(), NOW)
    args, kwargs = dependencies.create_run.call_args
    assert args[3].window_start == prior.params_json["window_start"]
    assert args[3].window_end == prior.params_json["window_end"]
    assert kwargs["resume_from_run_id"] == (prior.id if reason in {"budget", "stall"} else None)


async def test_successful_window_continues_without_gap(dependencies):
    conf, prior = config(), previous()
    dependencies.get_config.return_value = conf
    dependencies.create_run.return_value = SimpleNamespace(id=uuid4(), status="pending")
    mod._candidate_ids.return_value = [conf.id]
    mod._schedule_history.return_value = (False, prior)
    await mod.collect_due_runs(AsyncMock(), NOW)
    request = dependencies.create_run.call_args.args[3]
    assert request.window_start == datetime.fromisoformat(prior.params_json["window_end"])
    assert request.window_end == NOW


@pytest.mark.parametrize("active,same_bucket", [(True, False), (False, True)])
async def test_locked_recheck_prevents_overlap_and_same_interval_budget_reset(dependencies, active, same_bucket):
    conf = config()
    prior = (
        previous("budget", created_at=NOW, params_json=previous().params_json | {"evaluation_key": mod._bucket(NOW, 5)})
        if same_bucket
        else None
    )
    dependencies.get_config.return_value = conf
    mod._candidate_ids.return_value = [conf.id]
    mod._schedule_history.return_value = (active, prior)
    db = AsyncMock()
    await mod.collect_due_runs(db, NOW)
    dependencies.create_run.assert_not_awaited()
    db.commit.assert_awaited()
    mod.celery_app.send_task.assert_not_called()


async def test_long_unobserved_gap_is_explicit_stall(dependencies):
    conf = config()
    prior = previous(params_json=previous().params_json | {"window_end": (NOW - timedelta(days=32)).isoformat()})
    dependencies.get_config.return_value = conf
    mod._candidate_ids.return_value = [conf.id]
    mod._schedule_history.return_value = (False, prior)
    stats = await mod.collect_due_runs(AsyncMock(), NOW)
    assert stats["stalled"] == [
        {"tenant_id": str(TENANT), "config_id": str(conf.id), "reason": "window_gap_exceeds_limit"}
    ]
    dependencies.create_run.assert_not_awaited()


async def test_broker_failure_keeps_durable_job_for_next_tick(dependencies):
    run_id = uuid4()
    mod._recovery_ids.return_value = [run_id]
    mod.celery_app.send_task.side_effect = ConnectionError("secret connection string")
    first = await mod.collect_due_runs(AsyncMock(), NOW)
    mod.celery_app.send_task.side_effect = None
    second = await mod.collect_due_runs(AsyncMock(), NOW + timedelta(minutes=1))
    assert first["dispatch_failed"] == 1 and second["dispatched"] == 1
    assert "secret" not in str(first)
    assert mod.celery_app.send_task.call_count == 2


async def test_bounded_batches_disclose_unvisited_work(dependencies):
    mod._recovery_ids.return_value = [uuid4() for _ in range(201)]
    stats = await mod.collect_due_runs(AsyncMock(), NOW)
    assert stats["dispatched"] == 200
    assert stats["truncated"] is True and stats["run_scan_limit"] == 200


async def test_broker_timeout_is_bounded_and_safe(dependencies, monkeypatch):
    import asyncio

    mod._recovery_ids.return_value = [uuid4()]

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(mod.asyncio, "to_thread", slow)
    monkeypatch.setattr(mod, "_DISPATCH_TIMEOUT", 0.01)
    stats = await mod.collect_due_runs(AsyncMock(), NOW)
    assert stats["dispatch_failed"] == 1


async def test_tick_timeout_releases_transaction_and_reports_truncation(dependencies, monkeypatch):
    import asyncio

    async def slow(*args):
        await asyncio.sleep(1)

    mod._recovery_ids.side_effect = slow
    monkeypatch.setattr(mod, "_TICK_TIMEOUT", 0.01)
    db = AsyncMock()
    stats = await mod.collect_due_runs(db, NOW)
    assert stats["truncated"] is True
    db.rollback.assert_awaited_once()


async def test_tenant_start_rotates_after_a_slow_tenant_exhausts_tick(dependencies, monkeypatch):
    import asyncio

    second_tenant = uuid4()
    mod.feature_flag_service.list_tenants_with_flags.return_value = [TENANT, second_tenant]
    visits = []

    async def slow(db, tenant_id, now):
        visits.append(tenant_id)
        await asyncio.sleep(1)

    mod._recovery_ids.side_effect = slow
    monkeypatch.setattr(mod, "_TICK_TIMEOUT", 0.01)
    await mod.collect_due_runs(AsyncMock(), NOW)
    await mod.collect_due_runs(AsyncMock(), NOW + timedelta(minutes=1))
    assert set(visits) == {TENANT, second_tenant}


def test_real_broker_socket_timeout_does_not_hold_asyncio_shutdown(monkeypatch):
    import asyncio
    import socket
    import threading
    import time

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(1)
    stopped = threading.Event()

    def silent_broker():
        try:
            peer, _ = listener.accept()
            with peer:
                stopped.wait(2)
        except OSError:
            pass

    server = threading.Thread(target=silent_broker, daemon=True)
    server.start()
    original = mod.celery_app.connection_for_write

    def connection(**kwargs):
        return original(f"redis://127.0.0.1:{listener.getsockname()[1]}/0", **kwargs)

    monkeypatch.setattr(mod.celery_app, "connection_for_write", connection)
    monkeypatch.setattr(mod, "_BROKER_IO_TIMEOUT", 0.02, raising=False)
    monkeypatch.setattr(mod, "_DISPATCH_TIMEOUT", 0.05)
    stats = {"dispatched": 0, "dispatch_failed": 0}
    started = time.monotonic()
    try:
        asyncio.run(mod._dispatch(TENANT, uuid4(), stats))
        assert time.monotonic() - started < 0.5
        assert stats["dispatch_failed"] == 1
    finally:
        stopped.set()
        listener.close()
        server.join(1)


async def test_real_scheduler_persists_opted_in_scope_and_recovers_failed_publish(db, admin_user, monkeypatch):
    from app.schemas.transaction_runs import ConfigControl
    from app.services.transaction_ops import state_service as state
    from tests.conftest import enable_feature_flag
    from tests.test_transaction_ops_state_db import seed_config

    actor, _ = admin_user
    tenant_id = actor.tenant_id
    conf = await seed_config(db, tenant_id, actor)
    config_id = conf.id
    await enable_feature_flag(db, tenant_id, "celigo")
    await enable_feature_flag(db, tenant_id, "reconciliation")
    publish = Mock(side_effect=ConnectionError("private broker"))
    monkeypatch.setattr(mod.celery_app, "send_task", publish)
    now = datetime.now(timezone.utc)
    assert (await mod.collect_due_runs(db, now))["created"] == 0
    await state.control_config(
        db, tenant_id, config_id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
    )
    first = await mod.collect_due_runs(db, now)
    assert first["created"] == first["dispatch_failed"] == 1, first
    runs = await state.list_runs(db, tenant_id, config_id=config_id)
    run_id = runs[0].id
    assert len(runs) == 1 and runs[0].status == "pending" and runs[0].initiated_by is None
    assert runs[0].params_json["origin"] == "schedule"
    await db.rollback()
    assert (await state.get_run(db, tenant_id, run_id)).status == "pending"
    publish.side_effect = None
    second = await mod.collect_due_runs(db, now + timedelta(minutes=1))
    assert second["recovered"] == second["dispatched"] == 1 and second["created"] == 0, second
    assert publish.call_args.kwargs == {
        "kwargs": {"tenant_id": str(tenant_id), "run_id": str(run_id)},
        "queue": "recon",
        "retry": False,
        "retry_policy": {"max_retries": 0},
        "connection": ANY,
        "ignore_result": True,
    }


async def test_real_scheduler_resumes_budget_cursor_once_in_next_bucket(db, admin_user, monkeypatch):
    from app.schemas.transaction_runs import ConfigControl, ProgressUpdate
    from app.services.transaction_ops import state_service as state
    from tests.conftest import enable_feature_flag
    from tests.test_transaction_ops_state_db import seed_config

    actor, _ = admin_user
    tenant_id = actor.tenant_id
    conf = await seed_config(db, tenant_id, actor)
    config_id, interval = conf.id, conf.interval_minutes
    await state.control_config(
        db, tenant_id, config_id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
    )
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, tenant_id, flag)
    monkeypatch.setattr(mod.celery_app, "send_task", Mock())
    now = datetime.now(timezone.utc)
    assert (await mod.collect_due_runs(db, now))["created"] == 1
    prior = (await state.list_runs(db, tenant_id, config_id=config_id))[0]
    previous_scope = prior.params_json.copy()
    token = await state.claim_run(db, tenant_id, prior.id, now=now)
    await state.update_progress(
        db, tenant_id, prior.id, ProgressUpdate(progress_json={"next_page": 4}), lease_token=token, now=now
    )
    await state.finish_run(db, tenant_id, prior.id, "budget", lease_token=token, now=now)
    assert (await mod.collect_due_runs(db, now))["created"] == 0
    next_tick = now + timedelta(minutes=interval)
    result = await mod.collect_due_runs(db, next_tick)
    assert result["created"] == 1, result
    runs = await state.list_runs(db, tenant_id, config_id=config_id)
    assert len(runs) == 2
    continuation = next(run for run in runs if run.id != prior.id)
    assert continuation.progress_json == {"next_page": 4}
    assert continuation.params_json["window_start"] == previous_scope["window_start"]
    assert continuation.params_json["window_end"] == previous_scope["window_end"]
    assert continuation.params_json["evaluation_key"] != previous_scope["evaluation_key"]
    assert (await mod.collect_due_runs(db, next_tick))["created"] == 0


async def test_real_recovery_includes_expired_deadlines_and_excludes_live_leases(db, admin_user, monkeypatch):
    from app.schemas.transaction_runs import RunCreate
    from app.services.transaction_ops import state_service as state
    from tests.conftest import enable_feature_flag
    from tests.test_transaction_ops_state_db import seed_config

    actor, _ = admin_user
    tenant_id = actor.tenant_id
    conf = await seed_config(db, tenant_id, actor)
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, tenant_id, flag)
    now = datetime.now(timezone.utc)
    expired = await state.create_run(
        db,
        tenant_id,
        conf.id,
        RunCreate(evaluation_key="expired", order_references=["R123456789"]),
        actor=actor,
        now=now - timedelta(hours=25),
    )
    stale = await state.create_run(
        db,
        tenant_id,
        conf.id,
        RunCreate(origin="chat", evaluation_key="stale", order_references=["R123456788"]),
        actor=actor,
        now=now,
    )
    active = await state.create_run(
        db,
        tenant_id,
        conf.id,
        RunCreate(evaluation_key="active", order_references=["R123456787"]),
        actor=actor,
        now=now,
    )
    await state.claim_run(db, tenant_id, stale.id, now=now)
    stale.lease_until = now - timedelta(seconds=1)
    await db.commit()
    await state.claim_run(db, tenant_id, active.id, now=now)
    publish = Mock()
    monkeypatch.setattr(mod.celery_app, "send_task", publish)
    result = await mod.collect_due_runs(db, now)
    assert result["recovered"] == 2 and result["created"] == 0, result
    expected = sorted((expired, stale), key=lambda row: (row.created_at, row.id))
    assert [call.kwargs["kwargs"]["run_id"] for call in publish.call_args_list] == [str(row.id) for row in expected]
    assert await state.claim_run(db, tenant_id, expired.id, now=now) is None
    publish.reset_mock()
    assert (await mod.collect_due_runs(db, now))["recovered"] == 1
    assert publish.call_args.kwargs["kwargs"]["run_id"] == str(stale.id)


async def test_real_queries_do_not_recover_tenant_missing_one_flag(db, admin_user, admin_user_b, monkeypatch):
    from app.schemas.transaction_runs import RunCreate
    from app.services.transaction_ops import state_service as state
    from tests.conftest import enable_feature_flag
    from tests.test_transaction_ops_state_db import seed_config

    actor, _ = admin_user
    foreign, _ = admin_user_b
    for person, flags in ((actor, ("celigo", "reconciliation")), (foreign, ("celigo",))):
        conf = await seed_config(db, person.tenant_id, person)
        await state.create_run(
            db,
            person.tenant_id,
            conf.id,
            RunCreate(evaluation_key="manual", order_references=["R123456789"]),
            actor=person,
        )
        for flag in flags:
            await enable_feature_flag(db, person.tenant_id, flag)
    publish = Mock()
    monkeypatch.setattr(mod.celery_app, "send_task", publish)
    result = await mod.collect_due_runs(db, datetime.now(timezone.utc))
    assert result["tenants"] == result["recovered"] == 1, result
    assert publish.call_args.kwargs["kwargs"]["tenant_id"] == str(actor.tenant_id)
