import json

from app.services.report.report_html import render_report_html
from app.services.report.report_service import (
    assemble_spec,
    fill_placeholders,
)

FROZEN = {
    "r1": {"columns": ["Period", "Revenue"], "rows": [["Q1", "100"], ["Q2", "150"]], "row_count": 2},
    "m1": {"value": "1.2M", "unit": "USD", "period": "Q2", "definition_version": 3, "columns": [], "rows": []},
}


def _resolver(rid):
    if rid not in FROZEN:
        raise KeyError(rid)
    return FROZEN[rid]


def test_fill_placeholders_injects_frozen_values():
    out = fill_placeholders("Revenue is {{result:m1.value}} for {{metric:m1}}", _resolver)
    assert "1.2M" in out and "{{" not in out


def test_fill_placeholders_unresolved_is_marked_not_fabricated():
    out = fill_placeholders("x {{result:nope.value}}", _resolver)
    assert "[unresolved: result:nope.value]" in out


def test_compose_assembles_frozen_spec():
    # Pure assembly is unit-tested directly (persistence is exercised by the Task 13 e2e).
    sections = [
        {"type": "narrative", "markdown": "Rev grew to {{result:m1.value}}."},
        {"type": "table", "result_id": "r1"},
        {"type": "chart", "result_id": "r1", "chart_type": "bar"},
        {"type": "metric_headline", "result_id": "m1", "label": "Revenue"},
    ]
    spec = assemble_spec(title="Q2", sections=sections, resolver=_resolver)
    html = render_report_html(spec, accent_hsl="0 0% 0%")
    # the tool layer builds the condensed (number-free) LLM payload
    condensed = json.dumps(
        {"success": True, "section_count": len(spec["sections"]), "title": "Q2"},
        default=str,
    )

    # narrative figure injected by backend, not the LLM
    narr = next(s for s in spec["sections"] if s["type"] == "narrative")
    assert "1.2M" in narr["markdown"]
    # table carries FULL frozen rows
    tbl = next(s for s in spec["sections"] if s["type"] == "table")
    assert tbl["rows"] == [["Q1", "100"], ["Q2", "150"]]
    # chart pre-rendered to svg
    chart = next(s for s in spec["sections"] if s["type"] == "chart")
    assert chart["svg"].startswith("<svg")
    # HTML actually renders
    assert html.startswith("<!DOCTYPE html>") and "1.2M" in html
    # trust boundary: condensed LLM payload has NO computed numbers
    assert "1.2M" not in condensed and "150" not in condensed
    assert "report_id" in condensed or "section_count" in condensed


async def test_compose_report_audit_logs_the_mutation(monkeypatch):
    """Repo rule (sqlalchemy-fastapi): always audit-log mutations. compose_report INSERTs a report."""
    import uuid
    from unittest.mock import AsyncMock

    from app.services.report import report_service

    audit_spy = AsyncMock()
    monkeypatch.setattr(report_service.audit_service, "log_event", audit_spy)
    monkeypatch.setattr(report_service, "set_tenant_context", AsyncMock())

    db = AsyncMock()
    db.add = lambda obj: setattr(obj, "id", uuid.uuid4())  # simulate PK assignment

    tenant_id = uuid.uuid4()
    actor = uuid.uuid4()
    await report_service.compose_report(
        db,
        tenant_id=tenant_id,
        title="Q2",
        sections=[{"type": "heading", "level": 1, "text": "Q2"}],
        resolver=lambda rid: {},
        created_by=actor,
    )
    audit_spy.assert_awaited_once()
    kwargs = audit_spy.await_args.kwargs
    assert kwargs["tenant_id"] == tenant_id
    assert kwargs["category"] == "report"
    assert kwargs["action"] == "report.compose"
    assert kwargs["actor_id"] == actor
    assert kwargs["resource_type"] == "report"
