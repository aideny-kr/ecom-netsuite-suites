"""Task 2 -- Python port of the schedule humaniser and stall verdict.

Cases are ported verbatim from
`frontend/src/components/celigo/__tests__/schedule.test.ts` (same cron
strings, same expected labels/intervals/states) plus a handful of Python-only
additions called out in the brief (duplicate/out-of-range values, the naive
-datetime guard). See `app/services/celigo/run_state.py`'s module docstring
for the ported rules themselves."""

from datetime import datetime, timezone

import pytest

from app.services.celigo.run_state import ParsedSchedule, RunState, parse_schedule, run_state

HOURS = "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23"


# --- parse_schedule -- ported 1:1 from schedule.test.ts ---------------------


@pytest.mark.parametrize(
    ("schedule", "expected"),
    [
        pytest.param(
            f"? 5,20,35,50 {HOURS} ? * *",
            ParsedSchedule(
                kind="cron",
                cron=f"? 5,20,35,50 {HOURS} ? * *",
                interval_minutes=15,
                label="every 15 min",
                display="? 5,20,35,50 0…23 ? * *",
            ),
            id="minute-list-over-every-hour",
        ),
        pytest.param(
            f"? 5,35 {HOURS} ? * *",
            ParsedSchedule(
                kind="cron",
                cron=f"? 5,35 {HOURS} ? * *",
                interval_minutes=30,
                label="every 30 min",
                display="? 5,35 0…23 ? * *",
            ),
            id="two-minutes-an-hour",
        ),
        pytest.param(
            f"? 10 {HOURS} ? * *",
            ParsedSchedule(
                kind="cron",
                cron=f"? 10 {HOURS} ? * *",
                interval_minutes=60,
                label="hourly at :10",
                display="? 10 0…23 ? * *",
            ),
            id="single-minute-an-hour",
        ),
        pytest.param(
            "? 5 0,4,8,12,16,20 ? * *",
            ParsedSchedule(
                kind="cron",
                cron="? 5 0,4,8,12,16,20 ? * *",
                interval_minutes=240,
                label="every 4 h",
                display="? 5 0,4,8,12,16,20 ? * *",
            ),
            id="hour-list-evenly-spaced",
        ),
        pytest.param(
            "? 5 2,10,18 ? * *",
            ParsedSchedule(
                kind="cron", cron="? 5 2,10,18 ? * *", interval_minutes=480, label="3×/day", display="? 5 2,10,18 ? * *"
            ),
            id="hour-list-not-whole-hours-under-8h-but-unevenly-spaced",
        ),
        pytest.param(
            "? 5 6 ? * *",
            ParsedSchedule(
                kind="cron", cron="? 5 6 ? * *", interval_minutes=1440, label="daily 06:05", display="? 5 6 ? * *"
            ),
            id="single-hour-single-minute-is-daily",
        ),
        pytest.param(
            "? 0 */6 * * *",
            ParsedSchedule(
                kind="cron", cron="? 0 */6 * * *", interval_minutes=360, label="every 6 h", display="? 0 */6 ? * *"
            ),
            id="hours-step-N-with-star-day-of-month",
        ),
        pytest.param(
            "? 0,30 0,12 ? * *",
            ParsedSchedule(
                kind="cron", cron="? 0,30 0,12 ? * *", interval_minutes=690, label="4×/day", display="? 0,30 0,12 ? * *"
            ),
            id="item-12-minutes-count-on-an-hour-subset",
        ),
        pytest.param(
            "? 0 0,6,12,18 ? * *",
            ParsedSchedule(
                kind="cron",
                cron="? 0 0,6,12,18 ? * *",
                interval_minutes=360,
                label="every 6 h",
                display="? 0 0,6,12,18 ? * *",
            ),
            id="item-12-evenly-spaced-hourly-set-still-every-N-h",
        ),
        pytest.param(
            "? 0,30 0,6,12,18 ? * *",
            ParsedSchedule(
                kind="cron",
                cron="? 0,30 0,6,12,18 ? * *",
                interval_minutes=330,
                label="8×/day",
                display="? 0,30 0,6,12,18 ? * *",
            ),
            id="item-12-two-minutes-on-a-6-hourly-set-is-no-longer-every-6-h",
        ),
    ],
)
def test_parse_schedule_cron_cases(schedule: str, expected: ParsedSchedule) -> None:
    assert parse_schedule(schedule) == expected


def test_parse_schedule_none_and_empty_string_are_on_demand() -> None:
    assert parse_schedule(None) == ParsedSchedule(kind="on_demand")
    assert parse_schedule("") == ParsedSchedule(kind="on_demand")


def test_parse_schedule_unknown_shapes_are_shown_verbatim() -> None:
    assert parse_schedule("? 0-59/15 * ? * *") == ParsedSchedule(kind="unknown", raw="? 0-59/15 * ? * *")
    assert parse_schedule({"type": "everyN"}).kind == "unknown"
    assert parse_schedule(False).kind == "unknown"


# --- Python-only additions called out in the brief --------------------------


def test_parse_schedule_step_n_minutes() -> None:
    parsed = parse_schedule(f"? */15 {HOURS} ? * *")
    assert parsed.kind == "cron"
    assert parsed.interval_minutes == 15
    assert parsed.label == "every 15 min"


@pytest.mark.parametrize("hours_field", ["*", "0-23"])
def test_parse_schedule_star_and_0_23_both_mean_every_hour(hours_field: str) -> None:
    parsed = parse_schedule(f"? 5,20,35,50 {hours_field} ? * *")
    assert parsed.kind == "cron"
    assert parsed.interval_minutes == 15
    assert parsed.label == "every 15 min"
    assert parsed.display == "? 5,20,35,50 0…23 ? * *"


@pytest.mark.parametrize("day_of_month", ["?", "*"])
def test_parse_schedule_day_of_month_question_mark_or_star_accepted(day_of_month: str) -> None:
    parsed = parse_schedule(f"? 5 6 {day_of_month} * *")
    assert parsed.kind == "cron"
    assert parsed.label == "daily 06:05"


def test_parse_schedule_day_of_month_l_rejected() -> None:
    assert parse_schedule("? 5 6 L * *") == ParsedSchedule(kind="unknown", raw="? 5 6 L * *")


def test_parse_schedule_duplicates_and_out_of_range_values_are_dropped() -> None:
    # "70" is out of range for a minute field (0-59) and is dropped; "5" is
    # duplicated and collapses to one value. Circular gaps over {5, 20} on a
    # 60-minute period are 15 and 45 -- the longest is 45.
    parsed = parse_schedule(f"? 5,5,70,20 {HOURS} ? * *")
    assert parsed.kind == "cron"
    assert parsed.interval_minutes == 45
    assert parsed.label == "every 45 min"


def test_parse_schedule_all_values_out_of_range_is_unknown() -> None:
    parsed = parse_schedule(f"? 70,80 {HOURS} ? * *")
    assert parsed.kind == "unknown"
    assert parsed.raw == f"? 70,80 {HOURS} ? * *"


# --- run_state -- ported from stallState in schedule.test.ts ----------------

CRON15 = f"? 5,20,35,50 {HOURS} ? * *"
SYNC = datetime(2026, 9, 2, 18, 12, 0, tzinfo=timezone.utc)


def test_run_state_on_time_21_minutes_before_sync_on_15_minute_schedule() -> None:
    last = datetime(2026, 9, 2, 17, 51, 0, tzinfo=timezone.utc)
    assert run_state(schedule=CRON15, disabled=False, last_executed_at=last, as_of=SYNC) == RunState(
        state="on_time", interval_minutes=15
    )


def test_run_state_stalled_3_hours_before_sync_with_12_runs_missed() -> None:
    last = datetime(2026, 9, 2, 15, 12, 0, tzinfo=timezone.utc)
    assert run_state(schedule=CRON15, disabled=False, last_executed_at=last, as_of=SYNC) == RunState(
        state="stalled", missed_runs=12, interval_minutes=15
    )


def test_run_state_paused_never_stalled() -> None:
    last = datetime(2024, 4, 15, 0, 0, 0, tzinfo=timezone.utc)
    assert run_state(schedule=CRON15, disabled=True, last_executed_at=last, as_of=SYNC).state == "paused"


def test_run_state_on_demand_makes_no_stall_claim() -> None:
    last = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert run_state(schedule=None, disabled=False, last_executed_at=last, as_of=SYNC).state == "on_demand"


def test_run_state_unknown_cron_makes_no_stall_claim() -> None:
    last = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert run_state(schedule="? 0-59/15 * ? * *", disabled=False, last_executed_at=last, as_of=SYNC).state == "unknown"


def test_run_state_no_run_is_its_own_state() -> None:
    assert run_state(schedule=CRON15, disabled=False, last_executed_at=None, as_of=SYNC).state == "no_run"


def test_run_state_missing_as_of_is_unknown_not_wall_clock() -> None:
    last = datetime(2026, 9, 2, 15, 12, 0, tzinfo=timezone.utc)
    assert run_state(schedule=CRON15, disabled=False, last_executed_at=last, as_of=None).state == "unknown"


# --- Python-only additions called out in the brief --------------------------


def test_run_state_negative_age_is_unknown() -> None:
    # last_executed_at AFTER as_of -- a clock skew / bad data case, not a stall.
    last = datetime(2026, 9, 2, 19, 0, 0, tzinfo=timezone.utc)
    assert run_state(schedule=CRON15, disabled=False, last_executed_at=last, as_of=SYNC).state == "unknown"


def test_run_state_naive_last_executed_at_raises_value_error() -> None:
    naive = datetime(2026, 9, 2, 15, 12, 0)
    with pytest.raises(ValueError):
        run_state(schedule=CRON15, disabled=False, last_executed_at=naive, as_of=SYNC)


def test_run_state_naive_as_of_raises_value_error() -> None:
    last = datetime(2026, 9, 2, 15, 12, 0, tzinfo=timezone.utc)
    naive_as_of = datetime(2026, 9, 2, 18, 12, 0)
    with pytest.raises(ValueError):
        run_state(schedule=CRON15, disabled=False, last_executed_at=last, as_of=naive_as_of)
