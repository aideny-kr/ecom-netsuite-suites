"""Daily coverage alerts from saved evidence; no provider calls or dispatches."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

GRACE_HOURS = 8


def freshness(entity, *, daily_check_hour, now, configured_at=None):
    """A standing condition, cleared by verified coverage rather than old errors.

    The deadline belongs to the first missing coverage day, not to the latest
    expected day, run or heartbeat. Starting another run cannot move it. Retryable stops within their
    finite continuation window get the grace period; terminal stops do not.
    """
    coverage, schedule = entity["coverage"], entity["schedule"]
    result = {"state": "not_applicable", "reason": None, "deadline_at": None, "grace_hours": GRACE_HOURS}
    if schedule["kind"] != "daily":
        return result
    if not schedule["enabled"]:
        return result | {"state": "paused"}
    zone = ZoneInfo(schedule["timezone"])
    expected = datetime.fromisoformat(coverage["expected_until"]).astimezone(zone)
    completed = datetime.fromisoformat(coverage["completed_until"]) if coverage.get("completed_until") else None
    due_day = expected.date()
    if coverage["status"] != "up_to_date":
        if completed:
            due_day = min(due_day, completed.astimezone(zone).date() + timedelta(days=1))
        elif configured_at is not None:
            # Without any verified scan, preserve the first possible check's
            # deadline. A broken initial schedule must not get new grace daily.
            created = configured_at.astimezone(zone)
            due_day = created.date()
            if datetime.combine(due_day, time(daily_check_hour), zone) < created:
                due_day += timedelta(days=1)
    check = datetime.combine(due_day, time(daily_check_hour), zone).astimezone(timezone.utc)
    deadline = check + timedelta(hours=GRACE_HOURS)
    result["deadline_at"] = deadline.isoformat()
    if coverage["status"] == "up_to_date":
        return result | {"state": "healthy"}

    latest = entity["latest_schedule"]
    continuation = entity.get("continuation") or {}
    recovering = continuation.get("state") in ("eligible", "waiting_for_retry", "continued")
    active = any(run["origin"] in ("schedule", "recovery") for run in entity["active_runs"])
    # Only a failed window still missing from coverage is relevant. An old
    # failed attempt is not evidence that a newer window stopped.
    failed_end = datetime.fromisoformat(latest["window_end"]) if latest and latest.get("window_end") else None
    missing_window = failed_end is not None and (completed is None or failed_end > completed)
    stopped = (
        latest
        and latest["status"] == "finished"
        and (
            latest["termination_reason"] in ("error", "stall")
            or (latest["termination_reason"] == "budget" and continuation.get("state") == "blocked")
        )
    )
    if stopped and missing_window and not active and not recovering:
        return result | {"state": "alert", "reason": "daily_scan_stopped"}
    if now >= deadline:
        return result | {"state": "alert", "reason": "coverage_overdue"}
    return result | {"state": "within_grace", "reason": "daily_completion_pending"}
