# backend/tests/services/report/test_period_resolver.py
"""Tests for resolve_last_closed_period — the last CLOSED NetSuite accounting period.

Ground truth (live Framework tenant, queried 2026-08-05):
  - Jun 2026 -> closed='T'.  Jul 2026 -> closed='F' (ended on the calendar,
    NOT closed in NetSuite — books close days-to-weeks after month end).
  - Periods already exist through Dec 2026, all closed='F' — "newest period"
    is wrong.
  - An `Adjust 2025` row exists with isadjust='T' whose name isn't "Mon YYYY".
  - closed/isadjust/isquarter/isyear are the STRINGS 'T'/'F', not booleans.

`netsuite_suiteql.execute` is mocked at the module boundary (the precedent used
throughout this codebase — see test_metric_compute_integration.py and
test_financial_report_tool.py) with a tiny fake that actually APPLIES the
WHERE-clause predicates found in the generated SQL text to a fixture table, so
a resolver bug that drops a filter (e.g. forgetting `isadjust = 'F'`) changes
the fake's output and fails the test — not just a string-match on the query.
"""

from __future__ import annotations

import re
from datetime import date

from app.services.report.period_resolver import (
    ClosedPeriod,
    PeriodUnavailableReason,
    resolve_last_closed_period,
)

_TENANT_ID = "test-tenant"


def _fake_period_table(rows: list[dict]):
    """Build a fake `netsuite_suiteql.execute` that filters/orders/limits `rows`
    the way NetSuite would for the SQL text period_resolver actually sends —
    but ONLY applies a predicate if its literal text is present in the query,
    so a resolver bug that omits a filter is visible in the test output.
    """
    calls: list[str] = []

    async def fake_execute(params, context=None, **kwargs):
        query = params["query"]
        calls.append(query)
        filtered = list(rows)

        if "closed = 'T'" in query:
            filtered = [r for r in filtered if r["closed"] == "T"]
        elif "closed = 'F'" in query:
            filtered = [r for r in filtered if r["closed"] == "F"]

        if "isadjust = 'F'" in query:
            filtered = [r for r in filtered if r["isadjust"] == "F"]
        if "isquarter = 'F'" in query:
            filtered = [r for r in filtered if r["isquarter"] == "F"]
        if "isyear = 'F'" in query:
            filtered = [r for r in filtered if r["isyear"] == "F"]

        m = re.search(r"enddate > TO_DATE\('([\d-]+)', 'YYYY-MM-DD'\)", query)
        if m:
            threshold = date.fromisoformat(m.group(1))
            filtered = [r for r in filtered if date.fromisoformat(r["enddate"]) > threshold]

        reverse = "ORDER BY enddate DESC" in query
        filtered.sort(key=lambda r: r["enddate"], reverse=reverse)

        picked = filtered[:1]
        if not picked:
            return {"columns": ["periodname", "enddate"], "rows": [], "row_count": 0}
        return {
            "columns": ["periodname", "enddate"],
            "rows": [[r["periodname"], r["enddate"]] for r in picked],
            "row_count": 1,
        }

    return fake_execute, calls


_BASE_ROWS = [
    {
        "periodname": "Jan 2026",
        "enddate": "2026-01-31",
        "closed": "T",
        "isadjust": "F",
        "isquarter": "F",
        "isyear": "F",
    },
    {
        "periodname": "May 2026",
        "enddate": "2026-05-31",
        "closed": "T",
        "isadjust": "F",
        "isquarter": "F",
        "isyear": "F",
    },
    {
        "periodname": "Jun 2026",
        "enddate": "2026-06-30",
        "closed": "T",
        "isadjust": "F",
        "isquarter": "F",
        "isyear": "F",
    },
    {
        "periodname": "Jul 2026",
        "enddate": "2026-07-31",
        "closed": "F",
        "isadjust": "F",
        "isquarter": "F",
        "isyear": "F",
    },
    {
        "periodname": "Aug 2026",
        "enddate": "2026-08-31",
        "closed": "F",
        "isadjust": "F",
        "isquarter": "F",
        "isyear": "F",
    },
    {
        "periodname": "Dec 2026",
        "enddate": "2026-12-31",
        "closed": "F",
        "isadjust": "F",
        "isquarter": "F",
        "isyear": "F",
    },
]


async def test_newest_closed_period_selected(monkeypatch):
    """Jun 2026 (newest closed='T') wins over the older Jan/May 2026 closed rows."""
    fake, _calls = _fake_period_table(_BASE_ROWS)
    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", fake)

    result = await resolve_last_closed_period(db=object(), tenant_id=_TENANT_ID)

    assert isinstance(result, ClosedPeriod)
    assert result.resolved is True
    assert result.reason is None
    assert result.name == "Jun 2026"
    assert result.enddate == date(2026, 6, 30)


async def test_open_future_periods_excluded_from_closed_pick(monkeypatch):
    """Jul/Aug/Dec 2026 (closed='F', including future-dated ones) must never be
    picked as the closed period, even though they're newer by enddate."""
    fake, _calls = _fake_period_table(_BASE_ROWS)
    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", fake)

    result = await resolve_last_closed_period(db=object(), tenant_id=_TENANT_ID)

    assert result.name == "Jun 2026"
    assert result.name not in ("Jul 2026", "Aug 2026", "Dec 2026")


async def test_adjustment_row_excluded_even_when_closed_and_newest(monkeypatch):
    """isadjust='T' rows are adjustment postings, not real months — `Adjust 2025`-style
    rows must never be picked even if closed='T' and dated after the real closed month
    (this is the scenario a missing `isadjust = 'F'` filter would get wrong)."""
    rows = _BASE_ROWS + [
        {
            "periodname": "Adjust 2026",
            "enddate": "2026-07-15",  # after Jun 2026 — would win if isadjust filter is dropped
            "closed": "T",
            "isadjust": "T",
            "isquarter": "F",
            "isyear": "F",
        }
    ]
    fake, _calls = _fake_period_table(rows)
    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", fake)

    result = await resolve_last_closed_period(db=object(), tenant_id=_TENANT_ID)

    assert result.name == "Jun 2026"


async def test_quarter_rollup_excluded_even_when_closed_and_newest(monkeypatch):
    """isquarter='T' rollup rows must never be picked (missing isquarter='F' filter case)."""
    rows = _BASE_ROWS + [
        {
            "periodname": "Q2 2026",
            "enddate": "2026-08-15",  # newer than Jun 2026 — would win if isquarter filter is dropped
            "closed": "T",
            "isadjust": "F",
            "isquarter": "T",
            "isyear": "F",
        }
    ]
    fake, _calls = _fake_period_table(rows)
    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", fake)

    result = await resolve_last_closed_period(db=object(), tenant_id=_TENANT_ID)

    assert result.name == "Jun 2026"


async def test_year_rollup_excluded_even_when_closed_and_newest(monkeypatch):
    """isyear='T' rollup rows must never be picked (missing isyear='F' filter case)."""
    rows = _BASE_ROWS + [
        {
            "periodname": "2026",
            "enddate": "2026-09-15",  # newer than Jun 2026 — would win if isyear filter is dropped
            "closed": "T",
            "isadjust": "F",
            "isquarter": "F",
            "isyear": "T",
        }
    ]
    fake, _calls = _fake_period_table(rows)
    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", fake)

    result = await resolve_last_closed_period(db=object(), tenant_id=_TENANT_ID)

    assert result.name == "Jun 2026"


async def test_next_open_period_reported(monkeypatch):
    """The immediate next period after the closed one (Jul 2026, closed='F') is
    surfaced as the next-open period so the UI can say 'Jul 2026 is still open'."""
    fake, _calls = _fake_period_table(_BASE_ROWS)
    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", fake)

    result = await resolve_last_closed_period(db=object(), tenant_id=_TENANT_ID)

    assert result.next_open_name == "Jul 2026"
    assert result.next_open_enddate == date(2026, 7, 31)


async def test_no_next_open_when_closed_period_is_the_last_row(monkeypatch):
    """No later period at all -> next_open fields stay None (not an error)."""
    rows = [r for r in _BASE_ROWS if r["closed"] == "T"]  # drop every open/future row
    fake, _calls = _fake_period_table(rows)
    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", fake)

    result = await resolve_last_closed_period(db=object(), tenant_id=_TENANT_ID)

    assert result.name == "Jun 2026"
    assert result.next_open_name is None
    assert result.next_open_enddate is None


async def test_no_closed_period_returns_reason(monkeypatch):
    """Empty result set (nothing ever closed) -> the None-equivalent shape with
    reason=no_closed_period, never a raised exception."""
    rows = [r for r in _BASE_ROWS if r["closed"] == "F"]  # nothing closed at all
    fake, _calls = _fake_period_table(rows)
    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", fake)

    result = await resolve_last_closed_period(db=object(), tenant_id=_TENANT_ID)

    assert result.resolved is False
    assert result.reason == PeriodUnavailableReason.NO_CLOSED_PERIOD
    assert result.name is None
    assert result.enddate is None
    assert result.next_open_name is None


async def test_unsupported_period_name_returns_reason_not_a_coerced_value(monkeypatch):
    """A 13-period / 4-4-5 fiscal calendar or renamed period whose name doesn't match
    'Mon YYYY' must come back as the None-shape with reason=unsupported_period_name —
    NEVER reformatted/coerced into something that would pass validation downstream."""
    rows = [
        {
            "periodname": "P07 2026",
            "enddate": "2026-06-30",
            "closed": "T",
            "isadjust": "F",
            "isquarter": "F",
            "isyear": "F",
        }
    ]
    fake, _calls = _fake_period_table(rows)
    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", fake)

    result = await resolve_last_closed_period(db=object(), tenant_id=_TENANT_ID)

    assert result.resolved is False
    assert result.reason == PeriodUnavailableReason.UNSUPPORTED_PERIOD_NAME
    assert result.name is None
    assert result.enddate is None


async def test_suiteql_error_returns_unreachable_not_an_exception(monkeypatch):
    """A SuiteQL/connection failure must degrade to reason=unreachable — the caller
    can render 'Couldn't reach NetSuite', not get a 500."""

    async def failing_execute(params, context=None, **kwargs):
        return {"error": True, "message": "NetSuite request failed: connect timeout"}

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", failing_execute)

    result = await resolve_last_closed_period(db=object(), tenant_id=_TENANT_ID)

    assert result.resolved is False
    assert result.reason == PeriodUnavailableReason.UNREACHABLE
    assert result.name is None


async def test_next_open_lookup_failure_degrades_but_keeps_resolved_closed_period(monkeypatch):
    """If the FIRST query (closed period) succeeds but the SECOND query (next-open
    lookup) errors, the resolver must still return the closed period it already
    found — losing only the 'is a later period open' detail, not the whole result."""
    call_count = {"n": 0}

    async def flaky_execute(params, context=None, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {
                "columns": ["periodname", "enddate"],
                "rows": [["Jun 2026", "2026-06-30"]],
                "row_count": 1,
            }
        return {"error": True, "message": "timeout"}

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", flaky_execute)

    result = await resolve_last_closed_period(db=object(), tenant_id=_TENANT_ID)

    assert result.resolved is True
    assert result.name == "Jun 2026"
    assert result.enddate == date(2026, 6, 30)
    assert result.next_open_name is None
    assert result.next_open_enddate is None


async def test_first_query_predicate_includes_all_four_filters_and_orders_newest_first(monkeypatch):
    """SQL-shape pin: the closed-period query must literally carry all four filters
    (closed/isadjust/isquarter/isyear) plus newest-by-enddate ordering — the ground
    truth this whole resolver exists to encode."""
    fake, calls = _fake_period_table(_BASE_ROWS)
    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", fake)

    await resolve_last_closed_period(db=object(), tenant_id=_TENANT_ID)

    first_query = calls[0]
    assert "accountingperiod" in first_query
    assert "closed = 'T'" in first_query
    assert "isadjust = 'F'" in first_query
    assert "isquarter = 'F'" in first_query
    assert "isyear = 'F'" in first_query
    assert "ORDER BY enddate DESC" in first_query


def test_accepted_name_pattern_is_the_downstream_validator_s_own_regex():
    """CROSS-MODULE CONTRACT. The resolver's whole promise is that the name it
    returns will satisfy the period filter that consumes it — so it reuses
    `netsuite_financial_report`'s regex object rather than copying the pattern.
    A copy could drift silently and the resolver would start handing back names
    `build_period_filter` rejects, which is exactly the failure it exists to
    prevent. Pin the identity so a refactor there fails HERE, loudly.

    Note `playbooks.py` deliberately keeps its OWN looser `_PERIOD_NAME_RE`
    ("jun 2026" / "June 2026") for parsing operator input — that one is not
    this contract and must not be substituted for it.
    """
    from app.mcp.tools import netsuite_financial_report as nfr
    from app.services.report import period_resolver

    assert period_resolver._PERIOD_NAME_RE is nfr._PERIOD_NAME_RE
    # and it is the strict canonical form, not the operator-input parser
    assert nfr._PERIOD_NAME_RE.match("Jun 2026")
    assert not nfr._PERIOD_NAME_RE.match("June 2026")
    assert not nfr._PERIOD_NAME_RE.match("jun 2026")
