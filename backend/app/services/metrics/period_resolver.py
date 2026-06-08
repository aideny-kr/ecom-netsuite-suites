# backend/app/services/metrics/period_resolver.py
"""Deterministic NL-period-token -> (start, end) date bounds. Honors fiscal_year_start_month."""

import calendar
import re
from datetime import date

SUPPORTED_TOKENS = {
    "this_month",
    "last_month",
    "this_quarter",
    "last_quarter",
    "this_year",
    "last_year",
    "ytd",
    "fy",
}

_FY_ABS = re.compile(r"fy(\d{4})$")


class PeriodError(ValueError):
    pass


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = (year * 12 + (month - 1)) + delta
    return idx // 12, idx % 12 + 1


def _fiscal_quarter_start_month(today: date, fy_start: int, quarters_back: int) -> tuple[int, int]:
    # Anchor on the calendar year the current fiscal year began. During the
    # trailing calendar months (today.month < fy_start) the fiscal year started
    # the previous calendar year, so roll the anchor back — mirroring the
    # this_year/ytd branches. Without this the bounds land ~1 year in the future.
    fy_year = today.year if today.month >= fy_start else today.year - 1
    months_since_fy = (today.month - fy_start) % 12
    q_index = months_since_fy // 3  # 0..3 within current fiscal year
    start_month_offset = q_index * 3 - quarters_back * 3
    return _add_months(fy_year, fy_start, start_month_offset)


def resolve_period(token: str, *, fiscal_year_start_month: int, today: date) -> tuple[date, date]:
    t = token.strip().lower()
    # Config-drift guard: fiscal_year_start_month flows from tenant_configs, which has NO DB
    # CHECK constraint (models/tenant.py), and compute_metric coerces it via
    # `int(... or 1)` — which only rescues falsy 0/None, not an out-of-range value like 13 or
    # -1 from a data-entry/import bug. The this_year/last_year/ytd branches build
    # date(year, fiscal_year_start_month, 1) DIRECTLY (the month/quarter branches normalize via
    # _add_months' modulo), so an out-of-range month would bare-raise ValueError and ESCAPE
    # compute_metric's `except (ParamError, PeriodError)` handler — 500ing the chat turn
    # instead of the §9 number-free invalid_params refusal. Reject uniformly as PeriodError so
    # every period token degrades gracefully when a tenant's fiscal config is invalid.
    if not 1 <= fiscal_year_start_month <= 12:
        raise PeriodError(f"fiscal_year_start_month must be in 1..12, got {fiscal_year_start_month}")
    fy_abs = _FY_ABS.fullmatch(t)
    if t not in SUPPORTED_TOKENS and fy_abs is None:
        raise PeriodError(f"unsupported period token: {token}")

    if t == "fy":
        return resolve_period("this_year", fiscal_year_start_month=fiscal_year_start_month, today=today)

    if fy_abs:
        sy = int(fy_abs.group(1))
        # Catch the BASE ValueError (not a narrower type): date() bare-raises both
        # 'year N is out of range' (sy 0, or an end date that overflows to year 10000
        # for a non-Jan fiscal start at sy 9999) AND 'month must be in 1..12'
        # (config-drift fiscal_year_start_month). Narrowing this catch would silently
        # reintroduce a caller-controllable bare ValueError that 500s compute_metric
        # instead of returning the structured invalid_params refusal.
        try:
            start = date(sy, fiscal_year_start_month, 1)
            end_y, end_m = _add_months(sy, fiscal_year_start_month, 11)
            return start, _month_end(end_y, end_m)
        except ValueError as ex:
            raise PeriodError(f"year-out-of-range period token: {token}") from ex

    if t in ("this_month", "last_month"):
        y, m = _add_months(today.year, today.month, 0 if t == "this_month" else -1)
        return date(y, m, 1), _month_end(y, m)

    if t in ("this_quarter", "last_quarter"):
        y, m = _fiscal_quarter_start_month(today, fiscal_year_start_month, 0 if t == "this_quarter" else 1)
        end_y, end_m = _add_months(y, m, 2)
        return date(y, m, 1), _month_end(end_y, end_m)

    if t in ("this_year", "last_year"):
        start_year = today.year if today.month >= fiscal_year_start_month else today.year - 1
        if t == "last_year":
            start_year -= 1
        start = date(start_year, fiscal_year_start_month, 1)
        end_y, end_m = _add_months(start_year, fiscal_year_start_month, 11)
        return start, _month_end(end_y, end_m)

    # ytd: fiscal-year start through today
    start_year = today.year if today.month >= fiscal_year_start_month else today.year - 1
    return date(start_year, fiscal_year_start_month, 1), today
