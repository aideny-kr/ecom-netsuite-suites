"""Tests for ``app.services.schedule_service.format_beat_schedule`` (live-run
defect, brief G item 5).

The Scheduled Jobs list (``GET /jobs/schedules``, ``app/api/v1/jobs.py``) was
showing raw Celery schedule text for system jobs -- a plain interval renders
as its own repr ("60.0", "3600.0", "21600") and a ``celery.schedules.crontab``
renders as ``<crontab: 0 3 * * * (m/h/dM/MY/d)>`` -- because the endpoint
serialized the Beat entry's schedule object with ``str()``. This pure
function replaces that with a human phrase; the endpoint wiring itself is
covered separately in ``tests/test_jobs_api.py``.
"""

from __future__ import annotations

from datetime import timedelta

from celery.schedules import crontab

from app.services.schedule_service import format_beat_schedule

# ---------------------------------------------------------------------------
# Numeric intervals (int, float, timedelta) -- seconds.
# ---------------------------------------------------------------------------


def test_sixty_seconds_is_every_minute():
    assert format_beat_schedule(60.0) == "every minute"


def test_three_thousand_six_hundred_seconds_is_every_hour():
    assert format_beat_schedule(3600.0) == "every hour"


def test_twenty_one_thousand_six_hundred_seconds_is_every_six_hours():
    """The exact live-run shape: ``6 * 60 * 60`` is a plain ``int`` (21600),
    not wrapped in ``timedelta`` -- ``oracle-skill-reseed`` in
    ``app/workers/celery_app.py``."""
    assert format_beat_schedule(21600) == "every 6 h"


def test_three_hundred_seconds_is_every_five_min():
    assert format_beat_schedule(300.0) == "every 5 min"


def test_nine_hundred_seconds_is_every_fifteen_min():
    assert format_beat_schedule(900.0) == "every 15 min"


def test_a_non_round_interval_falls_back_to_seconds():
    assert format_beat_schedule(90.0) == "every 90 sec"


def test_timedelta_minutes_is_formatted_the_same_as_its_second_count():
    assert format_beat_schedule(timedelta(minutes=15)) == "every 15 min"


def test_timedelta_hours_is_formatted_the_same_as_its_second_count():
    assert format_beat_schedule(timedelta(hours=2)) == "every 2 h"


# ---------------------------------------------------------------------------
# Celery crontab objects.
# ---------------------------------------------------------------------------


def test_crontab_with_fixed_minute_and_hour_and_wildcard_days_is_daily():
    """``knowledge-crawler``'s real entry: ``crontab(hour=3, minute=0)``."""
    assert format_beat_schedule(crontab(hour=3, minute=0)) == "daily 03:00"


def test_crontab_daily_pads_single_digit_minute():
    assert format_beat_schedule(crontab(hour=4, minute=5)) == "daily 04:05"


def test_crontab_with_only_the_minute_fixed_is_hourly():
    """``report-auto-refresh-hourly``'s real entry: ``crontab(minute=10)`` —
    hour, day_of_month, month_of_year, and day_of_week are all left wildcard."""
    assert format_beat_schedule(crontab(minute=10)) == "hourly at :10"


def test_crontab_with_a_restricted_day_of_week_falls_back_to_cron_text():
    """Minute AND hour are both fixed here, but day_of_week is NOT wildcard
    (Monday only) -- this is neither the "daily" nor the "hourly" shape, so
    it must fall back to the raw cron field rendering rather than being
    misreported as "daily 00:00" (which would silently drop the "Monday
    only" restriction)."""
    result = format_beat_schedule(crontab(minute=0, hour=0, day_of_week=1))
    assert result == "cron 0 0 * * 1"


def test_crontab_fallback_renders_every_field_verbatim():
    result = format_beat_schedule(crontab(minute=30, hour=14, day_of_month=1))
    assert result == "cron 30 14 1 * *"


def test_crontab_with_only_minute_fixed_but_a_restricted_day_of_week_is_not_hourly():
    """Brief H, item 4: the OLD `_format_crontab` decided "hourly" from
    `len(hour) != 1` alone -- true for a full wildcard hour field (24
    elements) but ALSO true for `hour` left untouched at its wildcard default
    while day_of_week is restricted, silently reporting "hourly at :00" and
    dropping the "Monday only" restriction entirely. `hour` must be the FULL
    wildcard set AND every day field must be wildcard for the hourly shape."""
    result = format_beat_schedule(crontab(minute=0, day_of_week=1))
    assert result == "cron 0 * * * 1"


def test_crontab_with_only_minute_fixed_but_a_restricted_hour_set_is_not_hourly():
    """A `hour` field restricted to a SUBSET of hours (not a single value, but
    also not every hour) must not be reported as "hourly" either -- the old
    `len(hour) != 1` check treated any multi-value hour set as "not pinned",
    therefore "hourly", when it is really neither daily nor hourly."""
    result = format_beat_schedule(crontab(minute=5, hour="0,12"))
    assert result == "cron 5 0,12 * * *"


# ---------------------------------------------------------------------------
# Defensive fallback for anything unexpected.
# ---------------------------------------------------------------------------


def test_an_unrecognized_schedule_type_falls_back_to_str():
    assert format_beat_schedule("crontab(hour=3, minute=0)") == "crontab(hour=3, minute=0)"
