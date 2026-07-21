"""Playbooks — curated deterministic report recipes (no LLM in the loop).

Keys map 1:1 to netsuite_financial_report REPORT_TEMPLATES so numbers are
statement-grade GL aggregates, not ad-hoc reconstructions.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.report import Report
from app.services.report.playbooks import (
    PLAYBOOKS,
    build_playbook_recipe,
    compose_playbook_report,
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
        ("income_statement", {"period": "June 2026"}, "period"),
    ],
)
def test_build_rejects_bad_input(key, params, msg):
    with pytest.raises(ValueError, match=msg):
        build_playbook_recipe(key, params)


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
            PlaybookComposeRequest(params={"period": "June 2026"}),  # malformed: "Jun", not "June"
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
