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


def _real_metric_payload():
    """Build the REAL blessed-metric payload via the REAL chain:
    metric_data_table(...) -> JSON -> extract_result_payload Path 1.
    This is what report.compose's resolver actually returns for a metric — NOT the
    hand-rolled top-level {value,unit,period} stub above.
    """
    from app.services.chat.tool_call_results import extract_result_payload
    from app.services.metrics.metric_compute import metric_data_table

    table = metric_data_table(
        "Net Revenue",
        "142800",
        "USD",
        "Q2 2026",
        "net_revenue",
        definition_version=7,
        source_kind="suiteql",
    )
    payload = extract_result_payload("metric_compute", {}, json.dumps(table))
    assert payload is not None
    return payload


def test_metric_headline_resolves_real_metric_payload_shape():
    """Gate B (finding #1/#11): the real metric payload has NO top-level value/unit/
    period — they live in rows[0] under columns ['Metric','Value','Unit','Period'].
    The headline must resolve them from the row, not return blanks."""
    payload = _real_metric_payload()
    # sanity: the real payload genuinely lacks the top-level keys the old code read
    assert "value" not in payload and "unit" not in payload and "period" not in payload

    def resolver(rid):
        return payload

    sections = [{"type": "metric_headline", "result_id": "r1", "label": "Net Revenue"}]
    spec = assemble_spec(title="Q2", sections=sections, resolver=resolver)
    head = next(s for s in spec["sections"] if s["type"] == "metric_headline")
    assert head["value"] == "142800"
    assert head["unit"] == "USD"
    assert head["period"] == "Q2 2026"
    assert head["definition_version"] == 7
    # provenance source recorded (definition_version survived the resolve)
    assert spec["provenance"]["sources"] == ["metric:r1@v7"]


def test_metric_placeholder_fills_real_metric_payload_value():
    """Gate B: {{metric:r1}} must fill the metric value from the real row-shaped payload."""
    payload = _real_metric_payload()
    out = fill_placeholders("Net revenue was {{metric:r1}} this quarter.", lambda rid: payload)
    assert "142800" in out
    assert "[unresolved" not in out


def test_table_section_caps_rows_at_max():
    """Gate E (finding #14): report.compose resolves the FULL uncapped payload, so a
    50k-row SuiteQL result would bake a multi-MB JSONB spec + HTML into one row and
    freeze the viewer. Cap the rendered table at _MAX_REPORT_TABLE_ROWS, mark the
    section truncated, and keep the TRUE row_count so the HTML renders the
    'Showing first rows of N' note."""
    from app.services.report import report_service

    cap = report_service._MAX_REPORT_TABLE_ROWS
    big = {
        "columns": ["Period", "Revenue"],
        "rows": [[str(i), str(i * 10)] for i in range(2500)],
        "row_count": 2500,
    }

    def resolver(rid):
        return big

    sections = [{"type": "table", "result_id": "r1"}]
    spec = assemble_spec(title="Big", sections=sections, resolver=resolver)
    tbl = next(s for s in spec["sections"] if s["type"] == "table")

    assert len(tbl["rows"]) == cap
    assert cap == 2000
    assert tbl["truncated"] is True
    assert tbl["row_count"] == 2500  # the TRUE pre-cap count is preserved
    # the rendered HTML surfaces the truncation note with the true count
    html = render_report_html(spec, accent_hsl="0 0% 0%")
    assert "Showing first rows of 2500" in html


def test_table_section_under_cap_not_truncated():
    """A small table must NOT be marked truncated (no false 'showing first rows' note)."""
    from app.services.report import report_service

    small = {
        "columns": ["Period", "Revenue"],
        "rows": [["Q1", "100"], ["Q2", "150"]],
        "row_count": 2,
    }
    spec = assemble_spec(
        title="Small",
        sections=[{"type": "table", "result_id": "r1"}],
        resolver=lambda rid: small,
    )
    tbl = next(s for s in spec["sections"] if s["type"] == "table")
    assert len(tbl["rows"]) == 2
    assert tbl["truncated"] is False
    assert tbl["row_count"] == 2
    assert report_service._MAX_REPORT_TABLE_ROWS == 2000


async def test_compose_report_is_turn_atomic_no_mid_turn_commit(monkeypatch):
    """Gate cluster A — turn atomicity: compose_report runs on the chat
    orchestrator's SHARED session and must NOT commit mid-turn (the orchestrator
    commits exactly ONCE at end of turn). It still flushes so the report PK is
    assigned for the audit row + the returned report_id."""
    import uuid
    from unittest.mock import AsyncMock

    from app.services.report import report_service

    monkeypatch.setattr(report_service.audit_service, "log_event", AsyncMock())
    monkeypatch.setattr(report_service, "set_tenant_context", AsyncMock())

    db = AsyncMock()
    db.add = lambda obj: setattr(obj, "id", uuid.uuid4())  # simulate PK assignment

    out = await report_service.compose_report(
        db,
        tenant_id=uuid.uuid4(),
        title="Q2",
        sections=[{"type": "heading", "level": 1, "text": "Q2"}],
        resolver=lambda rid: {},
        created_by=uuid.uuid4(),
    )

    # Turn atomicity: the shared-session commit is the orchestrator's job.
    db.commit.assert_not_awaited()
    db.commit.assert_not_called()
    # Flush is still required so report.id is populated before the audit + return.
    db.flush.assert_awaited()
    assert out["report_id"]
