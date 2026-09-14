"""Schedule humaniser and stall verdict -- pure module, no DB, no I/O.

Ported from frontend/src/components/celigo/schedule.ts; keep the two in
step. That module's reasoning, kept verbatim here:

A schedule, humanised for the flow map/table. `"cron"` carries the raw
string back (`cron`) alongside the derived facts a caller needs to build a
pill or a stall check (`interval_minutes`), a short label ("every 15 min",
"hourly at :10", "3x/day", ...) and a compact `display` string (hour lists
collapse to "0...23" when the schedule runs every hour). `"on_demand"` is
Celigo's `schedule: null` (or an empty string). `"unknown"` is anything that
isn't the one six-field shape seen live -- shown verbatim in `raw`, never
guessed at.

Only the six-field shape `? <minutes> <hours> ? * *` seen live is parsed.
Minutes: a comma list or `*/N`. Hours: a comma list, `*`, `0-23`, or `*/N`.
Anything else is `unknown` and rendered verbatim -- a humaniser that guesses
becomes a check that lies.

The trailing three Quartz-style fields (day-of-month, month, day-of-week) on
every schedule seen live are `?`, `*`, `*` -- but a plain every-N-hours
schedule is sometimes written with day-of-month `*` instead of `?`, since
both mean "every day" once day-of-week is also `*`. Accepting either avoids
rejecting a real schedule as `unknown` over a Quartz day-of-month/day-of-week
convention nobody downstream cares about.

The interval is the longest gap between two ACTUAL runs, so on an hour
subset it has to be measured over the full minute-of-day set (every hour x
every minute), not over the hours alone. Measuring the hour gap ignores the
minute list entirely: "? 0,30 0,12" runs at 00:00, 00:30, 12:00 and 12:30,
whose longest gap is 11.5 h -- measuring hours alone would report 12 h.
`run_state` doubles this number to decide "stalled?", so an error here goes
straight into the health pill.

"every N h" is a claim of EVEN spacing on the hour, so it survives only when
every gap is the same whole number of hours that divides the day, and is
under 8 h. Anything else is just a count of runs ("N x/day").

Whether a flow's last run is on pace for its own schedule, checked against
the SYNC timestamp (`as_of`), never the wall clock: a 3-day-old snapshot
must not manufacture stalls just because nobody looked at it sooner.
`missed_runs`/`interval_minutes` are only ever present on
`"stalled"`/`"on_time"` respectively (both need the schedule to have parsed
to a known interval).

Python addition (the TS side takes ISO strings and lets `Date.parse`
silently produce `NaN` for a bad one): `last_executed_at` and `as_of` must
be timezone-aware `datetime`s when given. A naive `datetime` is a caller bug,
not a business state, so it raises `ValueError` rather than being folded
into `"unknown"`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

_STEP_RE = re.compile(r"^\*/(\d+)$")
_DIGIT_LIST_RE = re.compile(r"^\d+(,\d+)*$")


def _expand(field: str, max_value: int) -> list[int] | None:
    """Comma list or `*/N` (minutes) / comma list, `*`, `0-23`, `*/N` (hours)
    -- anything else is not a known field shape. Lists are de-duplicated,
    sorted, and out-of-range values dropped; an all-out-of-range list (or an
    empty one) is treated the same as an unparseable field: `None`."""
    if field == "*" or field == f"0-{max_value - 1}":
        return list(range(max_value))
    step_match = _STEP_RE.match(field)
    if step_match:
        n = int(step_match.group(1))
        return list(range(0, max_value, n)) if n > 0 else None
    if not _DIGIT_LIST_RE.match(field):
        return None
    values = [int(v) for v in field.split(",")]
    in_range = sorted({v for v in values if 0 <= v < max_value})
    return in_range or None


def _circular_gaps(values: list[int], period: int) -> list[int]:
    """Every circular gap between consecutive values, wrapping the last back
    to the first. A single value has exactly one gap: the whole period."""
    if len(values) == 1:
        return [period]
    gaps = [values[i + 1] - values[i] for i in range(len(values) - 1)]
    gaps.append(period - values[-1] + values[0])
    return gaps


def _max_gap(values: list[int], period: int) -> int:
    return max(_circular_gaps(values, period))


def _has_known_tail(parts: list[str]) -> bool:
    """The trailing three Quartz-style fields -- day-of-month, month,
    day-of-week -- must be (`?` or `*`), `*`, `*`."""
    if len(parts) != 6 or parts[0] != "?" or parts[4] != "*" or parts[5] != "*":
        return False
    return parts[3] in ("?", "*")


@dataclass(frozen=True)
class ParsedSchedule:
    kind: Literal["cron", "on_demand", "unknown"]
    cron: str | None = None
    raw: str | None = None
    interval_minutes: int | None = None
    label: str | None = None
    display: str | None = None


def parse_schedule(schedule: object) -> ParsedSchedule:
    if schedule is None or schedule == "":
        return ParsedSchedule(kind="on_demand")
    if not isinstance(schedule, str):
        return ParsedSchedule(kind="unknown", raw=json.dumps(schedule))
    parts = schedule.strip().split()
    if not _has_known_tail(parts):
        return ParsedSchedule(kind="unknown", raw=schedule)
    minutes = _expand(parts[1], 60)
    hours = _expand(parts[2], 24)
    if minutes is None or hours is None:
        return ParsedSchedule(kind="unknown", raw=schedule)

    all_hours = len(hours) == 24
    if all_hours:
        interval_minutes = _max_gap(minutes, 60)
        label = f"hourly at :{minutes[0]:02d}" if len(minutes) == 1 else f"every {interval_minutes} min"
    elif len(hours) == 1 and len(minutes) == 1:
        interval_minutes = 1440
        label = f"daily {hours[0]:02d}:{minutes[0]:02d}"
    else:
        minutes_of_day = sorted(h * 60 + m for h in hours for m in minutes)
        gaps = _circular_gaps(minutes_of_day, 1440)
        interval_minutes = max(gaps)
        evenly_spaced = all(g == gaps[0] for g in gaps)
        whole_hours = interval_minutes % 60 == 0 and 1440 % interval_minutes == 0 and interval_minutes < 8 * 60
        label = f"every {interval_minutes // 60} h" if evenly_spaced and whole_hours else f"{len(minutes_of_day)}×/day"

    display = f"? {parts[1]} {'0…23' if all_hours else parts[2]} ? * *"
    return ParsedSchedule(kind="cron", cron=schedule, interval_minutes=interval_minutes, label=label, display=display)


@dataclass(frozen=True)
class RunState:
    state: Literal["on_time", "stalled", "paused", "on_demand", "no_run", "unknown"]
    missed_runs: int | None = None
    interval_minutes: int | None = None


def run_state(
    *,
    schedule: object,
    disabled: bool | None,
    last_executed_at: datetime | None,
    as_of: datetime | None,
) -> RunState:
    if last_executed_at is not None and last_executed_at.tzinfo is None:
        raise ValueError("last_executed_at must be timezone-aware")
    if as_of is not None and as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")

    if disabled is True:
        return RunState(state="paused")

    parsed = parse_schedule(schedule)
    if parsed.kind == "on_demand":
        return RunState(state="on_demand")
    if parsed.kind == "unknown" or parsed.interval_minutes is None:
        return RunState(state="unknown")
    if last_executed_at is None:
        return RunState(state="no_run")
    if as_of is None:
        return RunState(state="unknown")

    age_minutes = (as_of - last_executed_at).total_seconds() / 60
    if age_minutes < 0:
        return RunState(state="unknown")
    if age_minutes > 2 * parsed.interval_minutes:
        missed_runs = int(age_minutes // parsed.interval_minutes)
        return RunState(state="stalled", missed_runs=missed_runs, interval_minutes=parsed.interval_minutes)
    return RunState(state="on_time", interval_minutes=parsed.interval_minutes)
