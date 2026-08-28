"""Playbooks — curated deterministic report recipes (no LLM in the loop).

Keys map 1:1 to netsuite_financial_report REPORT_TEMPLATES so numbers are
statement-grade GL aggregates, not ad-hoc reconstructions.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.report import Report
from app.models.report_series import ReportSeries
from app.services.report.period_resolver import ClosedPeriod, PeriodUnavailableReason
from app.services.report.playbooks import (
    PLAYBOOKS,
    PeriodUnavailableError,
    build_playbook_recipe,
    compose_playbook_report,
    normalize_period,
    prior_period,
    trailing_periods,
    yoy_period,
)
from app.services.report.refresh_service import RefreshError
from tests.conftest import create_test_tenant, create_test_user
from tests.fixtures import statement_fixture as fx


def test_catalog_lists_three_statement_playbooks_with_period_param():
    assert set(PLAYBOOKS) == {"income_statement", "balance_sheet", "trial_balance"}
    for meta in PLAYBOOKS.values():
        assert meta["name"] and meta["description"]
        assert [p["key"] for p in meta["params"]] == ["period"]


# ---------------------------------------------------------------------------
# Period math — pure calendar helpers over the validated "Mon YYYY" format.
# ---------------------------------------------------------------------------
def test_prior_period():
    assert prior_period("Jun 2026") == "May 2026"


def test_prior_period_crosses_year_boundary():
    assert prior_period("Jan 2026") == "Dec 2025"


def test_yoy_period():
    assert yoy_period("Jun 2026") == "Jun 2025"


def test_trailing_periods_six_months_chronological_includes_current():
    assert trailing_periods("Jun 2026", 6) == "Jan 2026,Feb 2026,Mar 2026,Apr 2026,May 2026,Jun 2026"


def test_trailing_periods_crosses_year_boundary():
    assert trailing_periods("Feb 2026", 3) == "Dec 2025,Jan 2026,Feb 2026"


def test_trailing_periods_single_month_is_just_the_period():
    assert trailing_periods("Jun 2026", 1) == "Jun 2026"


@pytest.mark.parametrize("bad", ["June 2026", "", "Jun26", "jun 2026", "Jun 26", "Xxx 2026"])
def test_prior_period_rejects_malformed_input(bad):
    with pytest.raises(ValueError, match="period"):
        prior_period(bad)


@pytest.mark.parametrize("bad", ["June 2026", "", "Xxx 2026"])
def test_yoy_period_rejects_malformed_input(bad):
    with pytest.raises(ValueError, match="period"):
        yoy_period(bad)


@pytest.mark.parametrize("bad", ["June 2026", "", "Xxx 2026"])
def test_trailing_periods_rejects_malformed_input(bad):
    with pytest.raises(ValueError, match="period"):
        trailing_periods(bad, 6)


def test_trailing_periods_validates_even_when_count_is_one():
    """count=1 never calls prior_period internally — the period itself must still be
    validated up front, not passed through unchecked."""
    with pytest.raises(ValueError, match="period"):
        trailing_periods("garbage", 1)


# ---------------------------------------------------------------------------
# normalize_period — forgiving human period input (operator-reported: "jun 2026" 400s
# outright on capitalization). Pure string mapping, no LLM, no calendar guessing beyond
# these exact unambiguous forms.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Jun 2026", "Jun 2026"),  # already canonical -- round-trips unchanged
        ("jun 2026", "Jun 2026"),  # lowercase 3-letter abbreviation
        ("JUN 2026", "Jun 2026"),  # uppercase 3-letter abbreviation
        ("June 2026", "Jun 2026"),  # full month name
        ("june 2026", "Jun 2026"),  # lowercase full month name
        ("JUNE 2026", "Jun 2026"),  # uppercase full month name
        ("6/2026", "Jun 2026"),  # numeric M/YYYY
        ("06/2026", "Jun 2026"),  # numeric MM/YYYY
        ("2026-06", "Jun 2026"),  # ISO YYYY-MM
        ("  jun 2026  ", "Jun 2026"),  # leading/trailing whitespace
        ("jun   2026", "Jun 2026"),  # multiple internal spaces
        ("December 2025", "Dec 2025"),  # a different month, full name
        ("1/2026", "Jan 2026"),  # numeric month boundary: January
        ("12/2026", "Dec 2026"),  # numeric month boundary: December
        ("2026-01", "Jan 2026"),  # ISO month boundary
        ("2026-12", "Dec 2026"),  # ISO month boundary
    ],
)
def test_normalize_period_accepts_all_documented_forms(raw, expected):
    assert normalize_period(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "13/2026",  # month out of range
        "0/2026",  # month out of range (no month 0)
        "jun 26",  # 2-digit year not accepted
        "2026",  # year only, no month
        "",  # empty
        "   ",  # whitespace only
        "Xxx 2026",  # not a real month name
        "2026-13",  # ISO month out of range
        "2026-00",  # ISO month out of range
        "June2026",  # no separator
        "13-2026",  # not ISO shape (only 2 digits before the dash)
    ],
)
def test_normalize_period_rejects_junk(bad):
    with pytest.raises(ValueError, match="period"):
        normalize_period(bad)


def test_normalize_period_rejects_non_string():
    with pytest.raises(ValueError, match="period"):
        normalize_period(None)


def test_normalize_period_error_message_is_short_and_names_accepted_forms():
    """Renders inline in the launcher -- must stay short and actionable."""
    with pytest.raises(ValueError) as exc:
        normalize_period("garbage")
    message = str(exc.value)
    assert len(message) < 100
    assert "Jun 2026" in message


# ---------------------------------------------------------------------------
# Recipe emission — sources + the financial_statement section, per playbook key.
# ---------------------------------------------------------------------------
def test_build_income_statement_recipe():
    title, recipe = build_playbook_recipe("income_statement", {"period": "Jun 2026"})
    assert "Jun 2026" in title
    assert recipe["schema_version"] == 1 and recipe["captured_at"]
    sources = recipe["sources"]
    assert set(sources) == {"r1", "r2", "r3", "r4"}
    assert sources["r1"] == {
        "tool": "netsuite_financial_report",
        "params": {"report_type": "income_statement", "period": "Jun 2026"},
        "connection_id": None,
    }
    assert sources["r2"] == {
        "tool": "netsuite_financial_report",
        "params": {"report_type": "income_statement", "period": "May 2026"},
        "connection_id": None,
    }
    assert sources["r3"] == {
        "tool": "netsuite_financial_report",
        "params": {"report_type": "income_statement", "period": "Jun 2025"},
        "connection_id": None,
    }
    assert sources["r4"] == {
        "tool": "netsuite_financial_report",
        "params": {
            "report_type": "income_statement_trend",
            "period": "Jan 2026,Feb 2026,Mar 2026,Apr 2026,May 2026,Jun 2026",
        },
        "connection_id": None,
    }
    # No "heading" section: the title already flows through assemble_spec's outer <h1>
    # (render_report_html emits it from spec["title"]) — a recipe-authored heading
    # section would duplicate it back-to-back in the rendered HTML. The old table +
    # narrative sections are gone too — financial_statement replaces both.
    assert recipe["sections"] == [
        {
            "type": "financial_statement",
            "result_id": "r1",
            "statement": "income_statement",
            "period": "Jun 2026",
            "compare": {"prior": "r2", "yoy": "r3", "trend": "r4"},
        }
    ]


@pytest.mark.parametrize("key", ["balance_sheet", "trial_balance"])
def test_build_prior_only_recipe(key):
    """balance_sheet/trial_balance compare only against the prior period in v1 — no
    yoy/trend sources or compare keys."""
    title, recipe = build_playbook_recipe(key, {"period": "Jun 2026"})
    assert "Jun 2026" in title
    sources = recipe["sources"]
    assert set(sources) == {"r1", "r2"}
    assert sources["r1"] == {
        "tool": "netsuite_financial_report",
        "params": {"report_type": key, "period": "Jun 2026"},
        "connection_id": None,
    }
    assert sources["r2"] == {
        "tool": "netsuite_financial_report",
        "params": {"report_type": key, "period": "May 2026"},
        "connection_id": None,
    }
    assert recipe["sections"] == [
        {
            "type": "financial_statement",
            "result_id": "r1",
            "statement": key,
            "period": "Jun 2026",
            "compare": {"prior": "r2"},
        }
    ]


@pytest.mark.parametrize(
    "key,params,msg",
    [
        ("nope", {"period": "Jun 2026"}, "Unknown playbook"),
        ("income_statement", {}, "period"),
        # "June 2026" (full month name) is a VALID normalize_period form now -- "13/2026"
        # (month out of range) replaces it as the still-invalid-post-normalization case.
        ("income_statement", {"period": "13/2026"}, "period"),
    ],
)
def test_build_rejects_bad_input(key, params, msg):
    with pytest.raises(ValueError, match=msg):
        build_playbook_recipe(key, params)


@pytest.mark.parametrize(
    "raw",
    ["jun 2026", "JUN 2026", "June 2026", "6/2026", "06/2026", "2026-06"],
)
def test_build_playbook_recipe_accepts_forgiving_period_input(raw):
    """The operator-reported bug: build_playbook_recipe used to 400 on anything but the
    exact canonical 'Mon YYYY' form. The canonical string must flow into the title, the
    section, AND every source's params (so SuiteQL receives exactly 'Jun 2026')."""
    title, recipe = build_playbook_recipe("income_statement", {"period": raw})
    assert "Jun 2026" in title
    assert recipe["sections"][0]["period"] == "Jun 2026"
    assert recipe["sources"]["r1"]["params"]["period"] == "Jun 2026"


_RESULT = json.dumps(
    {
        "success": True,
        "columns": ["acctnumber", "acctname", "accttype", "section", "amount"],
        "rows": [["4000", "Sales", "Income", "1-Revenue", 1000]],
        "row_count": 1,
        "query": "SELECT 1",
    }
)


def _patch_executor(monkeypatch, result_str=_RESULT, by_params=None):
    """Fake ``execute_tool_call``. With ``by_params`` (a ``{(report_type, period): result_str}``
    map) each call is served the result matching its OWN ``tool_input`` — needed once a
    recipe fans out to multiple sources with different report_type/period pairs; a call
    whose params aren't in the map falls back to ``result_str``. Always records every call
    (tool + params) for assertion, regardless of mode."""
    calls = []

    async def fake_execute(tool_name, tool_input, tenant_id, actor_id, correlation_id, db, **kw):
        calls.append({"tool": tool_name, "params": tool_input})
        if by_params is not None:
            key = (tool_input.get("report_type"), tool_input.get("period"))
            return by_params.get(key, result_str)
        return result_str

    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", fake_execute)
    return calls


def _raw_tool_result(payload: dict) -> str:
    """A statement_fixture EXTRACTED payload (columns/rows/row_count/query) reconstructed
    as the RAW netsuite_financial_report tool-result JSON string ``extract_result_payload``
    Path 1 (columns+rows) parses — see ``app/services/chat/tool_call_results.py``. A
    ``fx._failed(...)`` payload (``{"success": False, "error": ...}``) is already in that
    raw shape and passes through unchanged. ``default=str`` mirrors real SuiteQL
    serialization (amounts often arrive as strings, never through float — see
    ``report_html.fmt_amount``'s docstring) for the fixture's raw ``Decimal`` cells."""
    if payload.get("success") is False:
        return json.dumps(payload)
    return json.dumps(
        {
            "success": True,
            "columns": payload["columns"],
            "rows": payload["rows"],
            "row_count": payload["row_count"],
            "query": payload.get("query", ""),
        },
        default=str,
    )


_IS_TREND_PERIOD = "Jan 2026,Feb 2026,Mar 2026,Apr 2026,May 2026,Jun 2026"


def _income_statement_by_params(*, r1=None, r2=None, r3=None, r4=None) -> dict:
    """The 4-call ``by_params`` map for an income_statement recipe. Each of ``r1``..``r4``
    defaults to that rid's fixture payload (as the raw tool-result JSON via
    ``_raw_tool_result``); pass a raw JSON string (e.g. a failed-tool result) to override
    that ONE source without touching the others."""
    payloads = fx.income_statement_payloads()
    return {
        ("income_statement", "Jun 2026"): r1 or _raw_tool_result(payloads["r1"]),
        ("income_statement", "May 2026"): r2 or _raw_tool_result(payloads["r2"]),
        ("income_statement", "Jun 2025"): r3 or _raw_tool_result(payloads["r3"]),
        ("income_statement_trend", _IS_TREND_PERIOD): r4 or _raw_tool_result(payloads["r4"]),
    }


async def test_compose_playbook_income_statement_renders_full_statement(db, monkeypatch):
    """The full financial_statement assembly path (Task 4): recipe -> 4-source fan-out ->
    build_statement_model -> financial_statement renderer -> persisted Report row. This
    replaces the Task-1/2/3-era fail-closed placeholder now that the assembly seam
    (ComposeSection schema + assemble_spec wiring) is live."""
    tenant = await create_test_tenant(db, name="PlaybookStmtCorp")
    user, _ = await create_test_user(db, tenant)
    calls = _patch_executor(monkeypatch, by_params=_income_statement_by_params())

    report = await compose_playbook_report(
        db,
        playbook_key="income_statement",
        params={"period": "Jun 2026"},
        tenant_id=tenant.id,
        actor_id=user.id,
    )

    assert len(calls) == 4
    html = report.rendered_html
    assert html.count("<h1") == 1  # title's own h1, no recipe-authored heading duplicate
    assert "Net income" in html  # KPI card label
    assert 'class="fs-quad' in html  # the variance quad
    assert 'class="fs-stmt' in html  # the full statement table
    # provenance: all 4 sources appear automatically (recipe["sources"]), never hand-picked
    for rid in ("r1", "r2", "r3", "r4"):
        assert f"{rid} —" in html
    # every fixture account name renders somewhere in the statement
    for row in fx.income_statement_payloads()["r1"]["rows"]:
        assert row[1] in html  # acctname is column index 1
    # the persisted spec is JSON-clean (Risk 3): no raw Decimal survived into spec_json
    assert json.dumps(report.spec_json)
    model = next(s["model"] for s in report.spec_json["sections"] if s["type"] == "financial_statement")
    assert model["statement"] == "income_statement"
    assert model["prior_period"] == "May 2026"
    assert model["yoy_period"] == "Jun 2025"
    assert model["trend"]["periods"] == fx.EXPECTED_TREND_PERIODS
    # spark/trend values persisted as JSON-safe strings, never float
    assert all(isinstance(v, str) for v in model["kpis"][0]["spark"])


async def test_compose_playbook_lowercase_period_normalizes_before_dispatch(db, monkeypatch):
    """Operator-reported bug: 'jun 2026' (lowercase) 400'd outright. Now it normalizes to
    the canonical 'Jun 2026' BEFORE any source dispatches -- proven by reusing the SAME
    by_params fake keyed on canonical strings only: if normalization ran too late (or not
    at all), every dispatch would miss every by_params key and silently fall back to the
    single-row default fixture instead of the full income-statement one."""
    tenant = await create_test_tenant(db, name="LowercasePeriodCorp")
    user, _ = await create_test_user(db, tenant)
    calls = _patch_executor(monkeypatch, by_params=_income_statement_by_params())

    report = await compose_playbook_report(
        db,
        playbook_key="income_statement",
        params={"period": "jun 2026"},
        tenant_id=tenant.id,
        actor_id=user.id,
    )

    assert len(calls) == 4
    assert calls[0]["params"]["period"] == "Jun 2026"  # r1 dispatched with the CANONICAL form
    assert "Jun 2026" in report.spec_json["title"]
    model = next(s["model"] for s in report.spec_json["sections"] if s["type"] == "financial_statement")
    # prior/yoy/trend only resolve correctly if r2/r3/r4 were ALSO dispatched canonically
    assert model["prior_period"] == "May 2026"
    assert model["yoy_period"] == "Jun 2025"
    assert model["trend"]["periods"] == fx.EXPECTED_TREND_PERIODS


async def test_compose_playbook_income_statement_degrades_when_compare_sources_fail(db, monkeypatch):
    """Risk 2: r1 (current period) succeeds; r2/r3/r4 (prior/yoy/trend) all fail at the
    tool layer. The statement still composes — never fails closed on a compare-source
    outage — it just renders without any of the deltas/YoY/trend those sources feed."""
    tenant = await create_test_tenant(db, name="PlaybookDegradeCorp")
    user, _ = await create_test_user(db, tenant)
    failed = json.dumps({"success": False, "error": "No active NetSuite connection found"})
    calls = _patch_executor(monkeypatch, by_params=_income_statement_by_params(r2=failed, r3=failed, r4=failed))

    report = await compose_playbook_report(
        db,
        playbook_key="income_statement",
        params={"period": "Jun 2026"},
        tenant_id=tenant.id,
        actor_id=user.id,
    )

    assert len(calls) == 4  # every source still attempted — degrade, not skip
    model = next(s["model"] for s in report.spec_json["sections"] if s["type"] == "financial_statement")
    assert model["prior_period"] is None
    assert model["yoy_period"] is None
    assert model["trend"] is None
    kpis = {k["key"]: k for k in model["kpis"]}
    assert kpis["revenue"]["value"] == "$13,500,000"  # r1's own figure unaffected
    assert kpis["revenue"]["mom_delta"] is None
    assert kpis["revenue"]["yoy_pct"] is None
    assert kpis["revenue"]["spark"] is None
    assert "vs May 2026" not in report.rendered_html  # no prior chip when prior is unavailable


def _provenance_line(html: str, rid: str) -> str:
    return next(seg for seg in html.split("<div>") if seg.startswith(f"{rid} —"))


async def test_compose_playbook_provenance_shows_not_available_for_degraded_compare(db, monkeypatch):
    """T2 gate M1: r3 (yoy) fails at the tool layer -- the frozen "Sources & method"
    block must NOT claim r3 was executed (a false trust claim); r1/r2/r4 keep their
    normal 'executed ...' stamps. The in-statement watch chip (statement_builder's own
    half of M1) must also be present, proving the wiring is end-to-end."""
    tenant = await create_test_tenant(db, name="ProvenanceDegradeCorp")
    user, _ = await create_test_user(db, tenant)
    failed = json.dumps({"success": False, "error": "No active NetSuite connection found"})
    calls = _patch_executor(monkeypatch, by_params=_income_statement_by_params(r3=failed))

    report = await compose_playbook_report(
        db,
        playbook_key="income_statement",
        params={"period": "Jun 2026"},
        tenant_id=tenant.id,
        actor_id=user.id,
    )

    assert len(calls) == 4  # every source still attempted
    html = report.rendered_html
    assert "executed" in _provenance_line(html, "r1")
    assert "executed" in _provenance_line(html, "r2")
    r3_line = _provenance_line(html, "r3")
    assert "not available this run — comparison omitted" in r3_line
    assert "executed" not in r3_line
    assert "executed" in _provenance_line(html, "r4")
    assert "Year-over-year comparison unavailable this run" in html


async def test_compose_playbook_income_statement_zero_row_r1_fails_closed(db, monkeypatch):
    """T2 gate M2: r1 RESOLVES (extract_result_payload succeeds -- valid but EMPTY
    columns+rows) but build_statement_model raises (statement_builder._require_rows now
    rejects a zero-account statement) -- compose must fail closed (502), never publish a
    contentless statement. Nothing persisted."""
    tenant = await create_test_tenant(db, name="ZeroRowCorp")
    user, _ = await create_test_user(db, tenant)
    empty_r1 = json.dumps(
        {
            "success": True,
            "columns": ["acctnumber", "acctname", "accttype", "section", "amount"],
            "rows": [],
            "row_count": 0,
            "query": "SELECT 1",
        }
    )
    _patch_executor(monkeypatch, by_params=_income_statement_by_params(r1=empty_r1))

    with pytest.raises(RefreshError) as exc_info:
        await compose_playbook_report(
            db,
            playbook_key="income_statement",
            params={"period": "Jun 2026"},
            tenant_id=tenant.id,
            actor_id=user.id,
        )
    assert exc_info.value.status_code == 502
    assert "statement could not be built" in exc_info.value.detail

    result = await db.execute(select(Report).where(Report.tenant_id == tenant.id))
    assert result.scalars().all() == []


async def test_compose_playbook_income_statement_r1_failure_still_fails_closed(db, monkeypatch):
    """Risk 2's other half: the CURRENT-period source (r1) is still a hard dependency —
    its failure kills the whole compose exactly like before Task 4 (no partial/degraded
    statement is ever published)."""
    tenant = await create_test_tenant(db, name="PlaybookR1FailCorp")
    user, _ = await create_test_user(db, tenant)
    failed = json.dumps({"success": False, "error": "No active NetSuite connection found"})
    calls = _patch_executor(monkeypatch, by_params=_income_statement_by_params(r1=failed))

    with pytest.raises(RefreshError) as exc:
        await compose_playbook_report(
            db,
            playbook_key="income_statement",
            params={"period": "Jun 2026"},
            tenant_id=tenant.id,
            actor_id=user.id,
        )
    assert "No active NetSuite connection found" in exc.value.detail
    assert len(calls) == 1  # r1 (needed first) raises before r2-r4 ever dispatch
    await db.rollback()
    rows = (await db.execute(select(Report).where(Report.tenant_id == tenant.id))).scalars().all()
    assert rows == []


# --- Task 4 fix-loop (T2 review Important): the 2-source shape (balance_sheet /
# trial_balance — compare={"prior": "r2"} only, no yoy/trend) had ZERO end-to-end
# compose coverage; only income_statement's 4-source shape was exercised. Both playbook
# keys below share the SAME assembly seam (assemble_spec -> build_statement_model) but a
# regression scoped to the 2-source path (e.g. required_result_ids treating a
# financial_statement's sole compare rid as required) would have gone undetected by the
# income_statement tests alone. ------------------------------------------------------


def _balance_sheet_by_params(*, r1=None, r2=None) -> dict:
    payloads = fx.balance_sheet_payloads()
    return {
        ("balance_sheet", "Jun 2026"): r1 or _raw_tool_result(payloads["r1"]),
        ("balance_sheet", "May 2026"): r2 or _raw_tool_result(payloads["r2"]),
    }


def _trial_balance_by_params(*, r1=None, r2=None) -> dict:
    payloads = fx.trial_balance_payloads()
    return {
        ("trial_balance", "Jun 2026"): r1 or _raw_tool_result(payloads["r1"]),
        ("trial_balance", "May 2026"): r2 or _raw_tool_result(payloads["r2"]),
    }


async def test_compose_playbook_balance_sheet_renders_full_statement(db, monkeypatch):
    """The 2-source recipe shape (compare={"prior": "r2"} only — no yoy/trend sources)
    through the full assembly path: recipe -> 2-source fan-out -> build_statement_model
    -> financial_statement renderer -> persisted Report row."""
    tenant = await create_test_tenant(db, name="PlaybookBsCorp")
    user, _ = await create_test_user(db, tenant)
    calls = _patch_executor(monkeypatch, by_params=_balance_sheet_by_params())

    report = await compose_playbook_report(
        db,
        playbook_key="balance_sheet",
        params={"period": "Jun 2026"},
        tenant_id=tenant.id,
        actor_id=user.id,
    )

    assert len(calls) == 2  # only r1 + r2 — no yoy/trend sources for balance_sheet
    html = report.rendered_html
    assert html.count("<h1") == 1
    assert "Assets = Liabilities + Equity" in html  # the statement's own check row
    assert 'class="fs-check fs-good"' in html  # the fixture is balanced -> ok=True
    assert "Δ $" in html  # prior-period deltas present (has_prior=True)
    for rid in ("r1", "r2"):  # provenance x2 — exactly the recipe's own source count
        assert f"{rid} —" in html
    assert "r3 —" not in html and "r4 —" not in html  # never more than the recipe has
    for row in fx.balance_sheet_payloads()["r1"]["rows"]:
        assert row[1] in html  # every fixture account name renders (acctname = col 1)
    assert json.dumps(report.spec_json)  # Risk 3: JSON-clean
    model = next(s["model"] for s in report.spec_json["sections"] if s["type"] == "financial_statement")
    assert model["statement"] == "balance_sheet"
    assert model["prior_period"] == "May 2026"
    assert model["checks"][0]["ok"] is True


async def test_compose_playbook_trial_balance_renders_full_statement(db, monkeypatch):
    """Same 2-source shape, the OTHER statement type with no `section` column at all
    (a flat GL listing) — proves the assembly seam is statement-type-agnostic."""
    tenant = await create_test_tenant(db, name="PlaybookTbCorp")
    user, _ = await create_test_user(db, tenant)
    calls = _patch_executor(monkeypatch, by_params=_trial_balance_by_params())

    report = await compose_playbook_report(
        db,
        playbook_key="trial_balance",
        params={"period": "Jun 2026"},
        tenant_id=tenant.id,
        actor_id=user.id,
    )

    assert len(calls) == 2
    html = report.rendered_html
    assert html.count("<h1") == 1
    assert "Debits = Credits" in html  # the statement's own check row
    assert 'class="fs-check fs-good"' in html  # the fixture is in balance -> ok=True
    assert "Δ $" in html  # prior-period deltas present
    for rid in ("r1", "r2"):
        assert f"{rid} —" in html
    for row in fx.trial_balance_payloads()["r1"]["rows"]:
        assert row[1] in html
    assert json.dumps(report.spec_json)
    model = next(s["model"] for s in report.spec_json["sections"] if s["type"] == "financial_statement")
    assert model["statement"] == "trial_balance"
    assert model["prior_period"] == "May 2026"
    assert model["checks"][0]["ok"] is True


async def test_compose_playbook_balance_sheet_degrades_when_prior_source_fails(db, monkeypatch):
    """Risk 2 on the 2-source shape: r1 succeeds, r2 (the ONLY compare source) fails at
    the tool layer -- the statement still composes without deltas, rather than failing
    closed (r2 is balance_sheet's sole degradable rid, unlike income_statement's three)."""
    tenant = await create_test_tenant(db, name="PlaybookBsDegradeCorp")
    user, _ = await create_test_user(db, tenant)
    failed = json.dumps({"success": False, "error": "No active NetSuite connection found"})
    calls = _patch_executor(monkeypatch, by_params=_balance_sheet_by_params(r2=failed))

    report = await compose_playbook_report(
        db,
        playbook_key="balance_sheet",
        params={"period": "Jun 2026"},
        tenant_id=tenant.id,
        actor_id=user.id,
    )

    assert len(calls) == 2  # r2 still attempted — degrade, not skip
    html = report.rendered_html
    assert "Assets = Liabilities + Equity" in html  # the statement itself still renders
    assert "Δ $" not in html  # no prior column at all when prior is unavailable
    assert "vs May 2026" not in html  # no prior chip
    model = next(s["model"] for s in report.spec_json["sections"] if s["type"] == "financial_statement")
    assert model["prior_period"] is None
    kpis = {k["key"]: k for k in model["kpis"]}
    assert kpis["total_assets"]["value"] == "$6,550,000"  # r1's own figure unaffected
    assert kpis["total_assets"]["mom_delta"] is None


async def test_compose_playbook_source_failure_creates_nothing(db, monkeypatch):
    tenant = await create_test_tenant(db, name="PlaybookFailCorp")
    user, _ = await create_test_user(db, tenant)
    _patch_executor(monkeypatch, json.dumps({"success": False, "error": "No active NetSuite connection found"}))

    with pytest.raises(RefreshError) as exc:
        await compose_playbook_report(
            db,
            playbook_key="income_statement",
            params={"period": "Jun 2026"},
            tenant_id=tenant.id,
            actor_id=user.id,
        )
    assert "No active NetSuite connection found" in exc.value.detail
    await db.rollback()
    count = (await db.execute(select(Report).where(Report.tenant_id == tenant.id))).scalars().all()
    assert count == []


def test_playbook_routes_declared_before_dynamic_report_route():
    """FastAPI matches in declaration order — /playbooks after /{report_id}
    would be swallowed and 404. Guard the ordering statically."""
    from app.api.v1 import reports as reports_api

    paths = [r.path for r in reports_api.router.routes]
    playbook_idx = min(i for i, p in enumerate(paths) if "playbooks" in p)
    dynamic_idx = min(i for i, p in enumerate(paths) if "{report_id}" in p)
    assert playbook_idx < dynamic_idx


async def test_compose_playbook_endpoint_income_statement_returns_201_with_rendered_statement(db, monkeypatch):
    """One layer up from the service-level happy-path test: the endpoint now returns a
    real 201 ReportResponse for a financial_statement playbook, now that the assembly
    seam (Task 4) is wired — this used to be a fail-closed 400 before the ComposeSection
    schema/assemble_spec wiring landed."""
    from app.api.v1.reports import PlaybookComposeRequest, compose_playbook_endpoint

    tenant = await create_test_tenant(db, name="PlaybookApiCorp")
    user, _ = await create_test_user(db, tenant)
    tenant_id = tenant.id  # read before compose_playbook_endpoint's commit expires `tenant`
    calls = _patch_executor(monkeypatch, by_params=_income_statement_by_params())

    response = await compose_playbook_endpoint(
        "income_statement",
        PlaybookComposeRequest(params={"period": "Jun 2026"}),
        user=user,
        db=db,
    )
    assert "Jun 2026" in response.title
    assert response.has_recipe is True
    assert len(calls) == 4
    await db.rollback()
    rows = (await db.execute(select(Report).where(Report.tenant_id == tenant_id))).scalars().all()
    assert len(rows) == 1
    assert "Net income" in rows[0].rendered_html


async def test_compose_playbook_endpoint_unknown_key_is_404(db, monkeypatch):
    from app.api.v1.reports import PlaybookComposeRequest, compose_playbook_endpoint

    tenant = await create_test_tenant(db, name="PlaybookApi404Corp")
    user, _ = await create_test_user(db, tenant)
    _patch_executor(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await compose_playbook_endpoint(
            "nope",
            PlaybookComposeRequest(params={"period": "Jun 2026"}),
            user=user,
            db=db,
        )
    assert exc.value.status_code == 404


async def test_compose_playbook_endpoint_bad_params_is_400(db, monkeypatch):
    from app.api.v1.reports import PlaybookComposeRequest, compose_playbook_endpoint

    tenant = await create_test_tenant(db, name="PlaybookApi400Corp")
    user, _ = await create_test_user(db, tenant)
    _patch_executor(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await compose_playbook_endpoint(
            "income_statement",
            # "June 2026" now normalizes fine -- "13/2026" (month out of range) is the
            # still-invalid-post-normalization malformed input.
            PlaybookComposeRequest(params={"period": "13/2026"}),
            user=user,
            db=db,
        )
    assert exc.value.status_code == 400
    assert "period" in exc.value.detail


async def test_compose_playbook_endpoint_tool_failure_passes_through_refresh_error(db, monkeypatch):
    from app.api.v1.reports import PlaybookComposeRequest, compose_playbook_endpoint

    tenant = await create_test_tenant(db, name="PlaybookApi502Corp")
    user, _ = await create_test_user(db, tenant)
    _patch_executor(monkeypatch, json.dumps({"success": False, "error": "No active NetSuite connection found"}))

    with pytest.raises(HTTPException) as exc:
        await compose_playbook_endpoint(
            "income_statement",
            PlaybookComposeRequest(params={"period": "Jun 2026"}),
            user=user,
            db=db,
        )
    assert exc.value.status_code == 502
    assert "No active NetSuite connection found" in exc.value.detail


# Rendered-HTML coverage for the financial_statement section (provenance block,
# exactly-one <h1>, statement content) returns once Task 3/4 land the renderer and
# assembly seam — assemble_spec cannot produce rendered_html for this section type yet
# (see test_compose_playbook_income_statement_pending_renderer_fails_closed above).


# ---------------------------------------------------------------------------
# mode="tracking" (Task 3, rolling-period Stage 1): the period is resolved from
# NetSuite's own close state (period_resolver.resolve_last_closed_period) instead of
# being typed, and the compose links into a per-tenant, per-playbook ReportSeries.
# mode="period" (the default) stays today's behaviour end to end — those existing
# tests above are untouched — plus persisting `period` on the row.
# ---------------------------------------------------------------------------


def _patch_resolver(monkeypatch, closed: ClosedPeriod):
    """Fake ``resolve_last_closed_period``. Patched at the period_resolver module
    boundary (not app.services.report.playbooks) because compose_playbook_report
    imports it lazily inside the function body -- a `from X import Y` executed at
    call time reads the CURRENT `X.Y`, so patching the source module is what a
    monkeypatch set up before the call actually reaches."""

    async def fake_resolve(db, tenant_id):
        return closed

    monkeypatch.setattr("app.services.report.period_resolver.resolve_last_closed_period", fake_resolve)


_JUN_CLOSED = ClosedPeriod(name="Jun 2026", enddate=date(2026, 6, 30))
_JUL_CLOSED = ClosedPeriod(name="Jul 2026", enddate=date(2026, 7, 31))


def _trial_balance_by_params_for(period: str, prior: str, *, r1=None, r2=None) -> dict:
    """Like ``_trial_balance_by_params`` but for an arbitrary (period, prior) pair --
    needed to compose a SECOND tracking report a month later without re-fixturing the
    whole income_statement 4-source shape."""
    payloads = fx.trial_balance_payloads()
    return {
        ("trial_balance", period): r1 or _raw_tool_result(payloads["r1"]),
        ("trial_balance", prior): r2 or _raw_tool_result(payloads["r2"]),
    }


async def test_compose_playbook_tracking_ignores_caller_supplied_period(db, monkeypatch):
    """Tracking mode resolves the period server-side -- a caller-supplied
    params["period"] must never leak through and get composed instead."""
    tenant = await create_test_tenant(db, name="TrackingIgnoresParamCorp")
    user, _ = await create_test_user(db, tenant)
    _patch_resolver(monkeypatch, _JUN_CLOSED)
    calls = _patch_executor(monkeypatch, by_params=_income_statement_by_params())

    report = await compose_playbook_report(
        db,
        playbook_key="income_statement",
        params={"period": "Dec 1999"},  # must be ignored entirely
        tenant_id=tenant.id,
        actor_id=user.id,
        mode="tracking",
    )

    assert report.period == "Jun 2026"
    assert calls[0]["params"]["period"] == "Jun 2026"


async def test_compose_playbook_tracking_resolves_period_and_creates_series(db, monkeypatch):
    tenant = await create_test_tenant(db, name="TrackingCorp")
    user, _ = await create_test_user(db, tenant)
    _patch_resolver(monkeypatch, _JUN_CLOSED)
    calls = _patch_executor(monkeypatch, by_params=_income_statement_by_params())

    report = await compose_playbook_report(
        db,
        playbook_key="income_statement",
        params={},
        tenant_id=tenant.id,
        actor_id=user.id,
        mode="tracking",
    )

    assert len(calls) == 4  # composed exactly like today for the resolved period
    assert report.period == "Jun 2026"
    assert report.series_id is not None
    series = (await db.execute(select(ReportSeries).where(ReportSeries.tenant_id == tenant.id))).scalar_one()
    assert series.id == report.series_id
    assert series.playbook_key == "income_statement"
    assert series.created_by == user.id


async def test_compose_playbook_tracking_series_conflicting_row_reuses_existing_not_500(db, monkeypatch):
    """MINOR (T2 gate): compose_playbook_report's ReportSeries get-or-create must
    resolve via a real Postgres upsert (ON CONFLICT), not a bare SELECT-then-INSERT —
    two concurrent tracking composes for the same (tenant, playbook_key) both racing
    the old SELECT would both see 'no row', both INSERT, and the loser would violate
    uq_report_series_tenant_playbook as an unhandled IntegrityError -> 500 (the
    endpoint only catches ValueError/RefreshError; verified this raises on
    unmodified code via a SELECT-interception race simulation during development).

    MAJOR B (T2 gate, round 2) moved the get-or-create's actual INSERT to AFTER
    _execute_sources succeeds (see the orphan-series tests below) — a pre-flight
    read-only SELECT now runs first and would simply find a row seeded BEFORE this
    call, never reaching the INSERT/ON CONFLICT branch at all. So the racing writer
    must land its row DURING _execute_sources (from the fake tool executor) — after
    the pre-flight SELECT has already returned 'no row' but before the real INSERT —
    to still exercise the ON CONFLICT path this test is actually about. Same
    seed-the-conflicting-row idiom as test_put_active_conflicting_row_upserts_instead_of_500
    in test_dashboard_api.py for the analogous dashboard.py race, just relocated to
    where a genuine race would actually land now."""
    tenant = await create_test_tenant(db, name="SeriesConflictCorp")
    user, _ = await create_test_user(db, tenant)
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    winner_id: dict[str, object] = {}

    async def fake_execute(tool_name, tool_input, tenant_id, actor_id, correlation_id, db, **kw):
        if "id" not in winner_id:
            # Simulate a concurrent tracking compose landing its series row WHILE this
            # one is still executing sources — exactly the window the real ON CONFLICT
            # upsert (in compose_playbook_report) exists to resolve gracefully.
            winner = ReportSeries(tenant_id=tenant.id, playbook_key="income_statement", created_by=user.id)
            db.add(winner)
            await db.flush()
            winner_id["id"] = winner.id
        by_params = _income_statement_by_params()
        key = (tool_input.get("report_type"), tool_input.get("period"))
        return by_params.get(key, _RESULT)

    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", fake_execute)

    report = await compose_playbook_report(
        db, playbook_key="income_statement", params={}, tenant_id=tenant.id, actor_id=user.id, mode="tracking"
    )

    assert report.series_id == winner_id["id"]
    series_rows = (await db.execute(select(ReportSeries).where(ReportSeries.tenant_id == tenant.id))).scalars().all()
    assert len(series_rows) == 1  # get-or-create resolved to the existing row, not a duplicate


async def test_compose_playbook_tracking_report_conflicting_row_reuses_existing_not_500(db, monkeypatch):
    """MAJOR 1 (T2 gate, round 3): moving the Report creation to after
    _execute_sources (to stop orphaned series -- see the orphan-series test below)
    widened the window between the pre-flight "does a report already exist for this
    (series_id, period)?" SELECT above and the actual Report insert further down --
    _execute_sources is a full round of live NetSuite calls in between. Two
    concurrent mode="tracking" composes for the same tenant/playbook/period
    (double-click, retry-on-timeout, two tabs) both pass the pre-flight check, both
    insert, and the second violates the partial unique index
    uq_reports_series_id_period as an unhandled IntegrityError --
    compose_playbook_endpoint only catches ValueError/RefreshError, so it would
    surface as a 500.

    The asymmetry was the tell: the ReportSeries insert lower down got ON CONFLICT DO
    NOTHING + re-select with a documented rationale; the Report insert was a bare
    db.add(report). Mirrors
    test_compose_playbook_tracking_series_conflicting_row_reuses_existing_not_500, but
    seeds a FULL competing compose (its own series AND its own Report row) during
    _execute_sources -- the series get-or-create is already race-safe (see that test);
    this one is about the very next insert."""
    tenant = await create_test_tenant(db, name="ReportConflictCorp")
    user, _ = await create_test_user(db, tenant)
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    winner: dict[str, object] = {}

    async def fake_execute(tool_name, tool_input, tenant_id, actor_id, correlation_id, db, **kw):
        if "report_id" not in winner:
            # Simulate a concurrent tracking compose finishing ENTIRELY (its own
            # series get-or-create AND its own Report insert) while this one is
            # still executing sources -- exactly the widened window MAJOR 1 exists to
            # guard.
            series = ReportSeries(tenant_id=tenant.id, playbook_key="income_statement", created_by=user.id)
            db.add(series)
            await db.flush()
            other_report = Report(
                tenant_id=tenant.id,
                title="Income Statement",
                spec_json={},
                rendered_html="<html></html>",
                created_by=user.id,
                recipe_json={},
                period="Jun 2026",
                series_id=series.id,
            )
            db.add(other_report)
            await db.flush()
            winner["series_id"] = series.id
            winner["report_id"] = other_report.id
        by_params = _income_statement_by_params()
        key = (tool_input.get("report_type"), tool_input.get("period"))
        return by_params.get(key, _RESULT)

    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", fake_execute)

    report = await compose_playbook_report(
        db, playbook_key="income_statement", params={}, tenant_id=tenant.id, actor_id=user.id, mode="tracking"
    )

    assert report.id == winner["report_id"]
    assert report.series_id == winner["series_id"]
    report_rows = (
        (await db.execute(select(Report).where(Report.series_id == winner["series_id"], Report.period == "Jun 2026")))
        .scalars()
        .all()
    )
    assert len(report_rows) == 1  # get-or-create resolved to the existing row, not a duplicate/IntegrityError


async def test_compose_playbook_tracking_never_returns_another_tenants_report(db, monkeypatch):
    """T2 gate round 4: the pre-flight idempotency lookup filtered ReportSeries by
    tenant_id but the follow-up Report lookup keyed ONLY on (series_id, period) --
    no tenant predicate. That is safe only while every reports.series_id is
    guaranteed to point at a series of the SAME tenant, and nothing guarantees it:
    reports.series_id is a plain single-column FK to report_series.id (migration
    093), not a tenant-composite one, so Postgres will happily accept a row whose
    tenant_id and series' tenant_id disagree.

    This test constructs exactly that row -- and it is constructible here precisely
    BECAUSE the CI role is BYPASSRLS, so RLS is not standing behind the query to
    save it. Without an explicit tenant predicate the compose hands tenant A a
    report belonging to tenant B: a cross-tenant read, the repo's hardest
    invariant. The query filter is the guard, not RLS and not the FK."""
    tenant_a = await create_test_tenant(db, name="TenantAlpha")
    tenant_b = await create_test_tenant(db, name="TenantBeta")
    user_a, _ = await create_test_user(db, tenant_a)
    user_b, _ = await create_test_user(db, tenant_b)
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    # Tenant A owns the series...
    series_a = ReportSeries(tenant_id=tenant_a.id, playbook_key="income_statement", created_by=user_a.id)
    db.add(series_a)
    await db.flush()

    # ...but a row belonging to TENANT B points at it for the same period. The plain
    # FK permits this; only an explicit tenant predicate in the lookup excludes it.
    foreign_report = Report(
        tenant_id=tenant_b.id,
        title="Tenant B private income statement",
        spec_json={},
        rendered_html="<html>tenant B numbers</html>",
        created_by=user_b.id,
        recipe_json={},
        period="Jun 2026",
        series_id=series_a.id,
    )
    db.add(foreign_report)
    await db.flush()

    async def fake_execute(tool_name, tool_input, tenant_id, actor_id, correlation_id, db, **kw):
        by_params = _income_statement_by_params()
        key = (tool_input.get("report_type"), tool_input.get("period"))
        return by_params.get(key, _RESULT)

    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", fake_execute)

    # The achievable guarantee is NOT "compose succeeds": uq_reports_series_id_period is
    # keyed on (series_id, period) with no tenant column, so tenant B's row genuinely
    # occupies that slot and tenant A cannot insert there. Full enforcement needs a
    # tenant-composite FK/index (follow-up). What compose MUST guarantee, and does, is
    # that it never silently hands back the other tenant's report -- it fails loudly.
    with pytest.raises(ValueError, match="different tenant"):
        await compose_playbook_report(
            db, playbook_key="income_statement", params={}, tenant_id=tenant_a.id, actor_id=user_a.id, mode="tracking"
        )

    # And the foreign row is untouched -- no overwrite, no read-through.
    still_b = (await db.execute(select(Report).where(Report.id == foreign_report.id))).scalar_one()
    assert still_b.tenant_id == tenant_b.id
    assert "tenant B numbers" in (still_b.rendered_html or "")


async def test_compose_playbook_tracking_failed_compose_leaves_no_orphaned_series(db, monkeypatch):
    """MAJOR B (T2 gate, round 2): a failed tracking compose must never leave a
    ReportSeries row behind with zero linked reports. The series get-or-create used to
    run BEFORE _execute_sources — if a tool call inside _execute_sources commits the
    session mid-flight (a real production risk: OAuth token refresh, see
    netsuite_oauth_service.py, reached via get_valid_token) and compose then fails (a
    required rid's own RefreshError, or the financial_statement_resolution_error 502
    path), that mid-flight commit durably persisted the series INSERT even though the
    exception propagated before any Report row or report.compose audit event ever
    existed — a phantom series with zero reports, surfaced by GET /dashboard's
    published_series as trackable, that the user never successfully created.

    Reproduces the mid-flight commit with a fake tool executor that commits the
    session (RELEASE SAVEPOINT under this fixture's create_savepoint mode — folds
    prior writes into the outer scope, immune to a LATER rollback, exactly like a real
    Postgres COMMIT durably persisting work mid-request — see conftest.py's db fixture
    docstring) before failing r1 (the hard-dependency source), then rolls back — as the
    endpoint's own except ValueError/RefreshError path leaves the session — and asserts
    no series row survived that sequence."""
    tenant = await create_test_tenant(db, name="OrphanSeriesCorp")
    user, _ = await create_test_user(db, tenant)
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    async def fake_execute(tool_name, tool_input, tenant_id, actor_id, correlation_id, db, **kw):
        await db.commit()  # simulates get_valid_token's token-refresh commit, mid-flight
        return json.dumps({"success": False, "error": "No active NetSuite connection found"})

    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", fake_execute)

    with pytest.raises(RefreshError):
        await compose_playbook_report(
            db, playbook_key="income_statement", params={}, tenant_id=tenant.id, actor_id=user.id, mode="tracking"
        )
    await db.rollback()

    series_rows = (await db.execute(select(ReportSeries).where(ReportSeries.tenant_id == tenant.id))).scalars().all()
    assert series_rows == []  # no orphan: the series is never durably created before a report exists
    report_rows = (await db.execute(select(Report).where(Report.tenant_id == tenant.id))).scalars().all()
    assert report_rows == []


async def test_compose_playbook_tracking_second_compose_same_period_is_idempotent(db, monkeypatch):
    tenant = await create_test_tenant(db, name="TrackingIdempotentCorp")
    user, _ = await create_test_user(db, tenant)
    _patch_resolver(monkeypatch, _JUN_CLOSED)
    calls = _patch_executor(monkeypatch, by_params=_income_statement_by_params())

    first = await compose_playbook_report(
        db, playbook_key="income_statement", params={}, tenant_id=tenant.id, actor_id=user.id, mode="tracking"
    )
    second = await compose_playbook_report(
        db, playbook_key="income_statement", params={}, tenant_id=tenant.id, actor_id=user.id, mode="tracking"
    )

    assert second.id == first.id
    assert len(calls) == 4  # the second compose never redispatched a single source
    series_rows = (await db.execute(select(ReportSeries).where(ReportSeries.tenant_id == tenant.id))).scalars().all()
    assert len(series_rows) == 1  # get-or-create, not a second series
    report_rows = (await db.execute(select(Report).where(Report.tenant_id == tenant.id))).scalars().all()
    assert len(report_rows) == 1  # no duplicate report for the same (series, period)


async def test_compose_playbook_tracking_new_closed_period_rolls_the_series_forward(db, monkeypatch):
    """A later tracking compose, once NetSuite closes the NEXT period, creates a new
    report but reuses the SAME series -- the series tracks the playbook, not one period."""
    tenant = await create_test_tenant(db, name="TrackingRollCorp")
    user, _ = await create_test_user(db, tenant)

    _patch_resolver(monkeypatch, _JUN_CLOSED)
    _patch_executor(monkeypatch, by_params=_trial_balance_by_params_for("Jun 2026", "May 2026"))
    june = await compose_playbook_report(
        db, playbook_key="trial_balance", params={}, tenant_id=tenant.id, actor_id=user.id, mode="tracking"
    )

    _patch_resolver(monkeypatch, _JUL_CLOSED)
    _patch_executor(monkeypatch, by_params=_trial_balance_by_params_for("Jul 2026", "Jun 2026"))
    july = await compose_playbook_report(
        db, playbook_key="trial_balance", params={}, tenant_id=tenant.id, actor_id=user.id, mode="tracking"
    )

    assert july.id != june.id
    assert june.period == "Jun 2026" and july.period == "Jul 2026"
    assert july.series_id == june.series_id
    series_rows = (await db.execute(select(ReportSeries).where(ReportSeries.tenant_id == tenant.id))).scalars().all()
    assert len(series_rows) == 1


async def test_compose_playbook_period_mode_sets_period_column_but_no_series(db, monkeypatch):
    """mode="period" (the default -- omitted here, exactly like every pre-Task-3
    caller) keeps composing against the TYPED period; the only Task-3 change is that
    `period` now persists on the row, and no series is ever touched."""
    tenant = await create_test_tenant(db, name="PeriodModeCorp")
    user, _ = await create_test_user(db, tenant)
    _patch_executor(monkeypatch, by_params=_income_statement_by_params())

    report = await compose_playbook_report(
        db, playbook_key="income_statement", params={"period": "Jun 2026"}, tenant_id=tenant.id, actor_id=user.id
    )

    assert report.period == "Jun 2026"
    assert report.series_id is None
    series_rows = (await db.execute(select(ReportSeries).where(ReportSeries.tenant_id == tenant.id))).scalars().all()
    assert series_rows == []


@pytest.mark.parametrize(
    "reason",
    [
        PeriodUnavailableReason.NO_CLOSED_PERIOD,
        PeriodUnavailableReason.UNSUPPORTED_PERIOD_NAME,
        PeriodUnavailableReason.UNREACHABLE,
    ],
)
async def test_compose_playbook_tracking_unresolved_period_raises_named_reason(db, monkeypatch, reason):
    tenant = await create_test_tenant(db, name=f"Unresolved{reason.value}Corp")
    user, _ = await create_test_user(db, tenant)
    _patch_resolver(monkeypatch, ClosedPeriod(name=None, enddate=None, reason=reason))
    calls = _patch_executor(monkeypatch)

    with pytest.raises(PeriodUnavailableError) as exc:
        await compose_playbook_report(
            db, playbook_key="income_statement", params={}, tenant_id=tenant.id, actor_id=user.id, mode="tracking"
        )

    assert exc.value.reason == reason
    assert str(exc.value)  # a real message, never composed for a guessed period
    assert calls == []  # never dispatches a single source when the period can't be resolved
    rows = (await db.execute(select(Report).where(Report.tenant_id == tenant.id))).scalars().all()
    assert rows == []
    series_rows = (await db.execute(select(ReportSeries).where(ReportSeries.tenant_id == tenant.id))).scalars().all()
    assert series_rows == []  # never gets far enough to get-or-create a series either


async def test_compose_playbook_tracking_audit_log_includes_series_id(db, monkeypatch):
    tenant = await create_test_tenant(db, name="AuditSeriesCorp")
    user, _ = await create_test_user(db, tenant)
    _patch_resolver(monkeypatch, _JUN_CLOSED)
    _patch_executor(monkeypatch, by_params=_income_statement_by_params())

    report = await compose_playbook_report(
        db, playbook_key="income_statement", params={}, tenant_id=tenant.id, actor_id=user.id, mode="tracking"
    )

    event = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant.id,
                AuditEvent.action == "report.compose",
                AuditEvent.resource_id == str(report.id),
            )
        )
    ).scalar_one()
    assert event.payload["series_id"] == str(report.series_id)


async def test_compose_playbook_period_mode_audit_log_has_no_series_id(db, monkeypatch):
    tenant = await create_test_tenant(db, name="AuditNoSeriesCorp")
    user, _ = await create_test_user(db, tenant)
    _patch_executor(monkeypatch, by_params=_income_statement_by_params())

    report = await compose_playbook_report(
        db, playbook_key="income_statement", params={"period": "Jun 2026"}, tenant_id=tenant.id, actor_id=user.id
    )

    event = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant.id,
                AuditEvent.action == "report.compose",
                AuditEvent.resource_id == str(report.id),
            )
        )
    ).scalar_one()
    assert "series_id" not in event.payload


async def test_compose_playbook_endpoint_tracking_returns_period_and_series_id(db, monkeypatch):
    from app.api.v1.reports import PlaybookComposeRequest, compose_playbook_endpoint

    tenant = await create_test_tenant(db, name="EndpointTrackingCorp")
    user, _ = await create_test_user(db, tenant)
    _patch_resolver(monkeypatch, _JUN_CLOSED)
    _patch_executor(monkeypatch, by_params=_income_statement_by_params())

    response = await compose_playbook_endpoint(
        "income_statement", PlaybookComposeRequest(mode="tracking"), user=user, db=db
    )

    assert response.period == "Jun 2026"
    assert response.series_id is not None


async def test_compose_playbook_endpoint_tracking_unresolved_period_is_400_naming_reason(db, monkeypatch):
    from app.api.v1.reports import PlaybookComposeRequest, compose_playbook_endpoint

    tenant = await create_test_tenant(db, name="Unresolved400Corp")
    user, _ = await create_test_user(db, tenant)
    _patch_resolver(monkeypatch, ClosedPeriod(name=None, enddate=None, reason=PeriodUnavailableReason.NO_CLOSED_PERIOD))
    _patch_executor(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await compose_playbook_endpoint(
            "income_statement",
            PlaybookComposeRequest(mode="tracking"),
            user=user,
            db=db,
        )

    assert exc.value.status_code == 400
    # An operator-facing message, not a bare enum/reason code leaking to the UI.
    assert exc.value.detail
    assert "no_closed_period" not in exc.value.detail
    assert "NO_CLOSED_PERIOD" not in exc.value.detail


def test_playbook_compose_request_mode_defaults_to_period():
    from app.schemas.report import PlaybookComposeRequest

    assert PlaybookComposeRequest(params={"period": "Jun 2026"}).mode == "period"
    assert PlaybookComposeRequest(params={"period": "Jun 2026"}, mode="tracking").mode == "tracking"
