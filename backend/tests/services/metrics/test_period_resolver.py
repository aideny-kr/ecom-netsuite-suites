# backend/tests/services/metrics/test_period_resolver.py
from datetime import date

import pytest

from app.services.metrics.period_resolver import PeriodError, resolve_period


def test_last_month_calendar():
    s, e = resolve_period("last_month", fiscal_year_start_month=1, today=date(2026, 3, 15))
    assert (s, e) == (date(2026, 2, 1), date(2026, 2, 28))


def test_this_year_calendar():
    s, e = resolve_period("this_year", fiscal_year_start_month=1, today=date(2026, 3, 15))
    assert (s, e) == (date(2026, 1, 1), date(2026, 12, 31))


def test_last_quarter_fiscal_start_april():
    # FY starts April; on 2026-05-15 we are in FY-Q1 (Apr-Jun); last quarter = Jan-Mar 2026.
    s, e = resolve_period("last_quarter", fiscal_year_start_month=4, today=date(2026, 5, 15))
    assert (s, e) == (date(2026, 1, 1), date(2026, 3, 31))


def test_this_quarter_fiscal_start_april_trailing_months():
    # FY starts April; on 2026-03-15 we are in the trailing calendar months
    # (today.month 3 < fy_start 4), i.e. FY-Q4 (Jan-Mar) of the fiscal year
    # that began April 2025. this_quarter = Jan-Mar 2026, NOT a future-dated window.
    s, e = resolve_period("this_quarter", fiscal_year_start_month=4, today=date(2026, 3, 15))
    assert (s, e) == (date(2026, 1, 1), date(2026, 3, 31))


def test_last_quarter_fiscal_start_april_trailing_months():
    # FY starts April; on 2026-01-15 we are in FY-Q4 (Jan-Mar); last quarter = Oct-Dec 2025.
    s, e = resolve_period("last_quarter", fiscal_year_start_month=4, today=date(2026, 1, 15))
    assert (s, e) == (date(2025, 10, 1), date(2025, 12, 31))


def test_unknown_token_raises():
    with pytest.raises(PeriodError):
        resolve_period("since_the_dawn_of_time", fiscal_year_start_month=1, today=date(2026, 3, 15))


def test_fy_alias_equals_this_year():
    a = resolve_period("fy", fiscal_year_start_month=2, today=date(2026, 6, 1))
    b = resolve_period("this_year", fiscal_year_start_month=2, today=date(2026, 6, 1))
    assert a == b


def test_fy_absolute_year():
    # FY2024 with fiscal year starting February → 2024-02-01 .. 2025-01-31
    s, e = resolve_period("fy2024", fiscal_year_start_month=2, today=date(2026, 6, 1))
    assert s == date(2024, 2, 1)
    assert e == date(2025, 1, 31)


def test_fy_absolute_year_calendar_fiscal():
    # FY2024 with January fiscal start → full calendar 2024
    s, e = resolve_period("fy2024", fiscal_year_start_month=1, today=date(2026, 6, 1))
    assert s == date(2024, 1, 1)
    assert e == date(2024, 12, 31)
