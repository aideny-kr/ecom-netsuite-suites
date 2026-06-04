# backend/app/services/metrics/period_resolver.py
"""Deterministic NL-period-token -> (start, end) date bounds. Honors fiscal_year_start_month."""

import calendar
from datetime import date

SUPPORTED_TOKENS = {
    "this_month",
    "last_month",
    "this_quarter",
    "last_quarter",
    "this_year",
    "last_year",
    "ytd",
}


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
    if t not in SUPPORTED_TOKENS:
        raise PeriodError(f"unsupported period token: {token}")

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
