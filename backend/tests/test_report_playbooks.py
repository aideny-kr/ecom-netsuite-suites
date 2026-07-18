"""Playbooks — curated deterministic report recipes (no LLM in the loop).

Keys map 1:1 to netsuite_financial_report REPORT_TEMPLATES so numbers are
statement-grade GL aggregates, not ad-hoc reconstructions.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.models.report import Report
from app.services.report.playbooks import PLAYBOOKS, build_playbook_recipe, compose_playbook_report
from app.services.report.refresh_service import RefreshError
from tests.conftest import create_test_tenant, create_test_user


def test_catalog_lists_three_statement_playbooks_with_period_param():
    assert set(PLAYBOOKS) == {"income_statement", "balance_sheet", "trial_balance"}
    for meta in PLAYBOOKS.values():
        assert meta["name"] and meta["description"]
        assert [p["key"] for p in meta["params"]] == ["period"]


def test_build_income_statement_recipe():
    title, recipe = build_playbook_recipe("income_statement", {"period": "Jun 2026"})
    assert "Jun 2026" in title
    assert recipe["schema_version"] == 1 and recipe["captured_at"]
    src = recipe["sources"]["r1"]
    assert src["tool"] == "netsuite_financial_report"
    assert src["params"] == {"report_type": "income_statement", "period": "Jun 2026"}
    assert src["connection_id"] is None
    kinds = [s["type"] for s in recipe["sections"]]
    # No "heading" section: the title already flows through assemble_spec's outer <h1>
    # (render_report_html emits it from spec["title"]) — a recipe-authored heading
    # section would duplicate it back-to-back in the rendered HTML.
    assert "table" in kinds and "narrative" in kinds
    assert "heading" not in kinds


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


def _patch_executor(monkeypatch, result_str=_RESULT):
    calls = []

    async def fake_execute(tool_name, tool_input, tenant_id, actor_id, correlation_id, db, **kw):
        calls.append({"tool": tool_name, "params": tool_input})
        return result_str

    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", fake_execute)
    return calls


async def test_compose_playbook_creates_versioned_refreshable_report(db, monkeypatch):
    tenant = await create_test_tenant(db, name="PlaybookCorp")
    user, _ = await create_test_user(db, tenant)
    calls = _patch_executor(monkeypatch)

    report = await compose_playbook_report(
        db,
        playbook_key="income_statement",
        params={"period": "Jun 2026"},
        tenant_id=tenant.id,
        actor_id=user.id,
    )

    assert calls[0]["tool"] == "netsuite_financial_report"
    assert report.version == 1 and report.recipe_json is not None
    assert report.auto_refresh == "daily"  # server default → sweep picks it up
    assert "Jun 2026" in report.title and "Sales" in report.rendered_html
    audit = (
        await db.execute(
            text("SELECT count(*) FROM audit_events WHERE action='report.compose' AND resource_id=:rid"),
            {"rid": str(report.id)},
        )
    ).scalar()
    assert audit == 1


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


async def test_compose_playbook_endpoint_creates_report(db, monkeypatch):
    from app.api.v1.reports import PlaybookComposeRequest, compose_playbook_endpoint

    tenant = await create_test_tenant(db, name="PlaybookApiCorp")
    user, _ = await create_test_user(db, tenant)
    _patch_executor(monkeypatch)

    resp = await compose_playbook_endpoint(
        "income_statement",
        PlaybookComposeRequest(params={"period": "Jun 2026"}),
        user=user,
        db=db,
    )
    assert resp.version == 1 and "Jun 2026" in resp.title


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


async def test_playbook_report_embeds_provenance(db, monkeypatch):
    tenant = await create_test_tenant(db, name="ProvCorp")
    user, _ = await create_test_user(db, tenant)
    _patch_executor(monkeypatch)
    report = await compose_playbook_report(
        db,
        playbook_key="income_statement",
        params={"period": "Jun 2026"},
        tenant_id=tenant.id,
        actor_id=user.id,
    )
    assert "NetSuite GL statement template (SuiteQL)" in report.rendered_html
    assert "period=Jun 2026" in report.rendered_html


async def test_playbook_report_title_renders_exactly_once(db, monkeypatch):
    """The outer <h1> already comes from spec["title"] (assemble_spec -> render_report_html).
    A recipe-authored heading section duplicated it back-to-back — the recipe must not
    emit one."""
    tenant = await create_test_tenant(db, name="TitleOnceCorp")
    user, _ = await create_test_user(db, tenant)
    _patch_executor(monkeypatch)
    report = await compose_playbook_report(
        db,
        playbook_key="income_statement",
        params={"period": "Jun 2026"},
        tenant_id=tenant.id,
        actor_id=user.id,
    )
    assert report.rendered_html.count("<h1") == 1
    assert "Income Statement — Jun 2026" in report.rendered_html
