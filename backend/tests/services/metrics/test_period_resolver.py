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


def test_fy_year_zero_raises_period_error():
    # fy0000 → start = date(0, ...) which bare-raises 'year 0 is out of range'.
    # Must surface as PeriodError (a ValueError subclass), not a plain ValueError.
    with pytest.raises(PeriodError) as ei:
        resolve_period("fy0000", fiscal_year_start_month=1, today=date(2026, 3, 15))
    assert ei.type is PeriodError


def test_fy_max_year_end_overflow_raises_period_error():
    # fy9999 with a non-Jan fiscal start: the END date rolls into year 10000
    # (_add_months(9999, 4, 11)), which bare-raises 'year 10000 is out of range'.
    # A parsed-year range check would MISS this; the try/except catches it.
    with pytest.raises(PeriodError) as ei:
        resolve_period("fy9999", fiscal_year_start_month=4, today=date(2026, 3, 15))
    assert ei.type is PeriodError


def test_fy9999_calendar_fiscal_still_resolves():
    # fy9999 with January fiscal start stays entirely within year 9999 — must NOT regress.
    s, e = resolve_period("fy9999", fiscal_year_start_month=1, today=date(2026, 3, 15))
    assert (s, e) == (date(9999, 1, 1), date(9999, 12, 31))


def test_fy_bad_fiscal_month_raises_period_error():
    # Config drift: a malformed fiscal_year_start_month (13) makes date() bare-raise
    # 'month must be in 1..12'. The except clause must stay `except ValueError` (base)
    # so this is converted to PeriodError too — pins the catch breadth.
    with pytest.raises(PeriodError) as ei:
        resolve_period("fy2024", fiscal_year_start_month=13, today=date(2026, 3, 15))
    assert ei.type is PeriodError


@pytest.mark.parametrize("token", ["this_year", "last_year", "ytd", "this_month", "this_quarter"])
@pytest.mark.parametrize("bad_fy", [13, 0, -1])
def test_out_of_range_fiscal_year_start_month_raises_period_error(token, bad_fy):
    """REAL config-drift invariant (T2 multi-angle gate, MAJOR). fiscal_year_start_month
    flows from tenant_configs, which has no DB CHECK constraint, and compute_metric's
    `int(... or 1)` coercion only rescues falsy 0/None — an out-of-range value (e.g. 13 from
    a data-entry/import bug) reaches resolve_period. The this_year/last_year/ytd branches
    build date(year, fiscal_year_start_month, 1) directly, so a bare ValueError would escape
    compute_metric's `except (ParamError, PeriodError)` and 500 the chat turn instead of the
    number-free invalid_params refusal the fy_abs branch is hardened to produce. resolve_period
    MUST reject an out-of-range fiscal_year_start_month as PeriodError for every token.

    Pre-fix: this_year/last_year/ytd raise a bare ValueError (not caught); this_month/
    this_quarter silently succeed. Post-fix: all raise PeriodError."""
    with pytest.raises(PeriodError):
        resolve_period(token, fiscal_year_start_month=bad_fy, today=date(2026, 6, 8))
