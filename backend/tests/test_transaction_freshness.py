"""Freshness means completed daily evidence, never a heartbeat or financial signoff."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.services.transaction_ops.freshness import freshness

NOW = datetime(2026, 10, 1, 17, tzinfo=timezone.utc)  # 10AM PDT


def entity():
    return {
        "coverage": {
            "status": "behind",
            "expected_until": "2026-10-01T07:00:00+00:00",
            "completed_until": "2026-09-30T07:00:00+00:00",
        },
        "schedule": {"kind": "daily", "enabled": True, "timezone": "America/Los_Angeles"},
        "latest_schedule": None,
        "active_runs": [],
        "continuation": None,
    }


def stopped(e, reason="error"):
    e["latest_schedule"] = {
        "status": "finished",
        "termination_reason": reason,
        "window_end": "2026-10-01T07:00:00+00:00",
    }
    return e


def check(e, now=NOW):
    return freshness(e, daily_check_hour=9, now=now)


def test_normal_daily_completion_has_eight_elapsed_hours_not_midnight_deadline():
    e = entity()
    original = deepcopy(e)
    result = check(e)
    assert result == {
        "state": "within_grace",
        "reason": "daily_completion_pending",
        "deadline_at": "2026-10-02T00:00:00+00:00",
        "grace_hours": 8,
    }
    assert check(e, datetime(2026, 10, 1, 23, 59, 59, tzinfo=timezone.utc))["state"] == "within_grace"
    assert check(e, datetime(2026, 10, 2, tzinfo=timezone.utc))["reason"] == "coverage_overdue"
    assert e == original


@pytest.mark.parametrize("status", ["behind", "not_verified"])
def test_missing_coverage_alerts_at_deadline_even_if_worker_has_healthy_heartbeat(status):
    e = entity()
    e["coverage"]["status"] = status
    e["active_runs"] = [
        {"origin": "schedule", "execution_state": "running", "run_state_updated_at": "2026-10-02T00:00:00+00:00"}
    ]
    assert check(e, datetime(2026, 10, 2, tzinfo=timezone.utc))["reason"] == "coverage_overdue"


@pytest.mark.parametrize("reason", ["error", "stall"])
def test_terminal_daily_stop_alerts_before_grace_expires(reason):
    assert check(stopped(entity(), reason))["reason"] == "daily_scan_stopped"


@pytest.mark.parametrize("state", ["eligible", "waiting_for_retry", "continued"])
def test_bounded_automatic_recovery_gets_grace_but_cannot_extend_deadline(state):
    e = stopped(entity(), "budget")
    e["continuation"] = {"state": state}
    assert check(e)["state"] == "within_grace"
    assert check(e, datetime(2026, 10, 2, tzinfo=timezone.utc))["reason"] == "coverage_overdue"


def test_exhausted_retry_budget_alerts_without_requiring_a_new_heartbeat():
    e = stopped(entity(), "budget")
    e["continuation"] = {"state": "blocked", "reason": "read_retry_limit"}
    assert check(e)["reason"] == "daily_scan_stopped"


@pytest.mark.parametrize("origin", ["schedule", "recovery"])
def test_new_automatic_attempt_suppresses_old_stop_only_until_deadline(origin):
    e = stopped(entity())
    e["active_runs"] = [{"origin": origin}]
    assert check(e)["state"] == "within_grace"
    assert check(e, datetime(2026, 10, 2, tzinfo=timezone.utc))["state"] == "alert"


def test_manual_saved_review_is_not_a_fresh_daily_scan():
    e = stopped(entity())
    e["active_runs"] = [{"origin": "manual"}]
    assert check(e)["reason"] == "daily_scan_stopped"


def test_caught_up_clears_old_failure_and_paused_or_interval_schedules_do_not_alert():
    e = stopped(entity())
    e["coverage"]["status"] = "up_to_date"
    assert check(e)["state"] == "healthy"
    e["coverage"]["status"] = "behind"
    e["schedule"]["enabled"] = False
    assert check(e)["state"] == "paused"
    e["schedule"]["kind"] = "interval"
    assert check(e)["state"] == "not_applicable"


def test_failed_window_already_covered_does_not_trigger_terminal_alert():
    e = stopped(entity())
    e["latest_schedule"]["window_end"] = e["coverage"]["completed_until"]
    assert check(e)["state"] == "within_grace"


def test_before_cutoff_uses_previous_days_unmet_deadline():
    e = entity()
    e["coverage"]["expected_until"] = "2026-09-30T07:00:00+00:00"
    e["coverage"]["completed_until"] = "2026-09-29T07:00:00+00:00"
    assert check(e, datetime(2026, 10, 1, 15, tzinfo=timezone.utc))["reason"] == "coverage_overdue"


def test_multiple_days_behind_do_not_receive_new_grace_at_each_daily_cutoff():
    e = entity()
    e["coverage"]["completed_until"] = "2026-09-29T07:00:00+00:00"
    assert check(e)["reason"] == "coverage_overdue"  # 10AM PDT, yesterday's deadline already missed
    assert check(e)["deadline_at"] == "2026-10-01T00:00:00+00:00"
    e["active_runs"] = [{"origin": "schedule"}]
    e["coverage"]["expected_until"] = "2026-10-02T07:00:00+00:00"
    assert check(e, datetime(2026, 10, 2, 17, tzinfo=timezone.utc))["deadline_at"] == "2026-10-01T00:00:00+00:00"
    e["coverage"]["completed_until"] = "2026-10-01T07:00:00+00:00"
    assert check(e, datetime(2026, 10, 2, 17, tzinfo=timezone.utc))["state"] == "within_grace"


def test_no_verified_scan_retains_its_first_eligible_check_deadline():
    e = entity()
    e["coverage"] |= {"status": "not_verified", "completed_until": None}
    result = freshness(e, daily_check_hour=9, now=NOW, configured_at=datetime(2026, 9, 29, 18, tzinfo=timezone.utc))
    assert result["deadline_at"] == "2026-09-30T02:00:00+00:00"  # Created11AM; eligible in current cycle
    assert result["state"] == "alert"


def test_new_config_created_after_daily_check_gets_initial_grace():
    e = entity()
    e["coverage"] |= {"status": "not_verified", "completed_until": None}
    result = freshness(e, daily_check_hour=9, now=NOW, configured_at=NOW)
    assert result["deadline_at"] == "2026-10-02T01:00:00+00:00"  # Eight hours from creation at10AM
    assert result["state"] == "within_grace"


@pytest.mark.parametrize(
    "day,midnight,deadline",
    [("2026-11-01", "07:00", "2026-11-02T01:00:00+00:00"), ("2026-03-08", "08:00", "2026-03-09T00:00:00+00:00")],
)
def test_dst_uses_local_check_time_then_elapsed_grace(day, midnight, deadline):
    e = entity()
    e["coverage"]["expected_until"] = f"{day}T{midnight}:00+00:00"
    e["coverage"]["completed_until"] = None
    assert check(e)["deadline_at"] == deadline


def test_non_pacific_check_hour_is_respected():
    e = entity()
    e["schedule"]["timezone"] = "Australia/Sydney"
    e["coverage"]["expected_until"] = "2026-09-30T14:00:00+00:00"
    e["coverage"]["completed_until"] = None
    assert freshness(e, daily_check_hour=6, now=NOW)["deadline_at"] == "2026-10-01T04:00:00+00:00"
