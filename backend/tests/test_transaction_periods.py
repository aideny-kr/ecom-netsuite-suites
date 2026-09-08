from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.services.transaction_ops import periods, scheduler

NOW = datetime(2026, 9, 8, 18, tzinfo=timezone.utc)


def test_last_week_is_the_same_closed_local_calendar_cohort():
    window = periods.review_window("last_week", NOW, "America/Los_Angeles")
    assert window["window_start"] == datetime(2026, 8, 31, 7, tzinfo=timezone.utc)
    assert window["window_end"] == datetime(2026, 9, 7, 7, tzinfo=timezone.utc)
    assert window["window_basis"] == "completed_at"


def test_last_month_preserves_dst_instead_of_subtracting_30_days():
    window = periods.review_window("last_month", datetime(2026, 4, 2, 18, tzinfo=timezone.utc), "America/Los_Angeles")
    assert window["window_start"] == datetime(2026, 3, 1, 8, tzinfo=timezone.utc)
    assert window["window_end"] == datetime(2026, 4, 1, 7, tzinfo=timezone.utc)


def test_yesterday_can_have_25_hours_and_custom_end_is_inclusive():
    now = datetime(2026, 11, 2, 18, tzinfo=timezone.utc)
    window = periods.review_window("yesterday", now, "America/Los_Angeles")
    assert window["window_end"] - window["window_start"] == timedelta(hours=25)
    custom = periods.review_window(
        "custom", now, "America/Los_Angeles", start_date=date(2026, 11, 1), end_date=date(2026, 11, 1)
    )
    assert custom == window


@pytest.mark.parametrize(
    "kind,zone,changes",
    [
        ("future", "America/Los_Angeles", {}),
        ("yesterday", "bad/timezone", {}),
        ("custom", "UTC", {"start_date": date(2026, 9, 8), "end_date": date(2026, 9, 8)}),
        ("custom", "UTC", {"start_date": date(2026, 1, 1), "end_date": date(2026, 9, 7)}),
    ],
)
def test_unsupported_future_and_unbounded_periods_rejected(kind, zone, changes):
    with pytest.raises(ValueError):
        periods.review_window(kind, NOW, zone, **changes)


def test_daily_window_has_sync_grace_overlap_and_a_successful_watermark():
    policy = periods.ReconciliationPolicy(timezone_name="America/Los_Angeles")
    last = datetime(2026, 9, 7, 7, tzinfo=timezone.utc)
    start, end = periods.scheduled_window(policy, NOW, last)
    assert start == last - timedelta(days=1)
    assert end == datetime(2026, 9, 8, 7, tzinfo=timezone.utc)
    assert periods.scheduled_window(policy, NOW, end) is None


def test_before_daily_check_hour_waits_and_long_outages_catch_up_in_slices():
    policy = periods.ReconciliationPolicy(timezone_name="America/Los_Angeles")
    last = datetime(2026, 9, 7, 7, tzinfo=timezone.utc)
    assert periods.scheduled_window(policy, datetime(2026, 9, 8, 15, tzinfo=timezone.utc), last) is None
    old = datetime(2026, 7, 1, 7, tzinfo=timezone.utc)
    start, end = periods.scheduled_window(policy, NOW, old)
    assert start == old - timedelta(days=1)
    assert end == old + timedelta(days=1)  # no skipped history after a >31-day outage


def test_scheduler_uses_the_policy_but_preserves_failed_window():
    config = SimpleNamespace(
        interval_minutes=1440, mapping_json={"reconciliation_policy": {"timezone_name": "America/Los_Angeles"}}
    )
    latest = SimpleNamespace(
        id="run",
        termination_reason="done",
        params_json={"window_start": "2026-06-30T07:00:00+00:00", "window_end": "2026-07-01T07:00:00+00:00"},
    )
    scope, previous, reason = scheduler._scope(config, latest, NOW)
    assert reason is None and scope["window_end"] == datetime(2026, 7, 2, 7, tzinfo=timezone.utc)
    latest.termination_reason = "budget"
    scope, previous, reason = scheduler._scope(config, latest, NOW)
    assert scope["window_end"] == latest.params_json["window_end"] and previous == "run"


def test_policy_rejects_unbounded_cost_and_invalid_timezones():
    for changes in [
        {"overlap_minutes": 10081},
        {"max_slice_days": 32},
        {"daily_check_hour": 24},
        {"timezone_name": "bad/timezone"},
    ]:
        with pytest.raises(ValidationError):
            periods.ReconciliationPolicy(**changes)


def test_calendar_period_can_cross_31_days_plus_dst_hour():
    from app.schemas.transaction_runs import RunCreate

    window = periods.review_window("last_month", datetime(2026, 11, 2, 18, tzinfo=timezone.utc), "Europe/Amsterdam")
    assert window["window_end"] - window["window_start"] == timedelta(days=31, hours=1)
    request = RunCreate(evaluation_key="october-close", **window)
    assert request.window_basis == "completed_at"
    with pytest.raises(ValidationError):
        RunCreate(evaluation_key="wrong-basis", order_references=["R000000001"], window_basis="completed_at")


@pytest.mark.asyncio
async def test_default_window_work_key_is_compatible_with_existing_runs(db, admin_user):
    from app.schemas.transaction_runs import RunCreate
    from app.services.transaction_ops import state_service
    from tests.test_transaction_ops_state_db import seed_config

    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    request = RunCreate(evaluation_key="legacy-retry", order_references=["R000000001"])
    expected = state_service.business_digest(
        {"config": config.config_key, "params": request.model_dump(exclude={"window_basis"})}
    )
    run = await state_service.create_run(db, actor.tenant_id, config.id, request, actor=actor)
    assert run.work_key == expected
    assert "window_basis" not in run.params_json


@pytest.mark.asyncio
async def test_completed_period_requires_replica_reader_and_is_part_of_identity(db, admin_user):
    from app.schemas.transaction_runs import RunCreate
    from app.services.transaction_ops import state_service
    from tests.test_transaction_ops_state_db import seed_config

    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    with pytest.raises(state_service.StateError, match="period_reader_unavailable"):
        await state_service.create_run(
            db,
            actor.tenant_id,
            config.id,
            RunCreate(
                evaluation_key="period",
                window_start=NOW - timedelta(days=7),
                window_end=NOW,
                window_basis="completed_at",
            ),
            actor=actor,
        )


@pytest.mark.asyncio
async def test_successful_catchup_slice_is_due_again_inside_same_schedule_bucket(db, admin_user):
    from app.schemas.transaction_runs import RunCreate
    from app.services.transaction_ops import state_service
    from tests.test_transaction_ops_state_db import seed_config

    actor = admin_user[0]
    config = await seed_config(
        db,
        actor.tenant_id,
        actor,
        schedule_enabled=True,
        interval_minutes=1440,
        mapping_json={"reference_field": "tranid", "reconciliation_policy": {"timezone_name": "America/Los_Angeles"}},
    )
    run = await state_service.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(
            origin="schedule",
            evaluation_key="catchup",
            window_start=NOW - timedelta(days=10),
            window_end=NOW - timedelta(days=9),
        ),
        actor=None,
        now=NOW,
    )
    run.status = "finished"
    run.termination_reason = "done"
    run.finished_at = NOW
    await db.flush()
    assert config.id in await scheduler._candidate_ids(db, actor.tenant_id, NOW)
