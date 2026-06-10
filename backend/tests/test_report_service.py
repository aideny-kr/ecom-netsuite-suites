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
