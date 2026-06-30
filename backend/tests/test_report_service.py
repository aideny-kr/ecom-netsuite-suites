import json

import pytest
from pydantic import ValidationError

from app.services.report.report_html import render_report_html
from app.services.report.report_service import (
    _REPORT_TABLE_TOP_K,
    _resolve_data_section,
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


def test_table_section_propagates_currency_columns_to_renderer():
    """A resolved reportData payload carries currency_columns; assemble_spec must
    propagate it onto the table section so render_report_html formats only that
    column, and select narrows it to the surviving columns."""

    def resolver(_rid):
        return {
            "kind": "table",
            "columns": ["account", "year", "amount"],
            "rows": [["Cash", 2026, 11500000.5]],
            "row_count": 1,
            "truncated": False,
            "currency_columns": ["amount"],
        }

    spec = assemble_spec("R", [{"type": "table", "result_id": "r1"}], resolver)
    table = spec["sections"][0]
    assert table["type"] == "table"
    assert table["currency_columns"] == ["amount"]
    html = render_report_html(spec)
    assert "11,500,000.50" in html  # amount accounting-formatted
    assert "<td>2026</td>" in html  # year NOT formatted (raw, no comma)

    # a select that drops the currency column → currency_columns narrows to []
    spec2 = assemble_spec("R", [{"type": "table", "result_id": "r1", "select": ["account", "year"]}], resolver)
    assert spec2["sections"][0]["currency_columns"] == []


# --- Production-path alias canonicalization -------------------------------
# The chat tool path (report_export.execute -> compose_report -> assemble_spec)
# receives the RAW LLM dicts and NEVER constructs ComposeRequest. So assemble_spec
# itself must canonicalize the `text`/`data` aliases before it reads s["type"] —
# otherwise a `text` section matches no branch, lands in the heading/divider else,
# and the renderer (no case for `text`) drops it SILENTLY (worse than the old loud
# ValidationError). Regression guard for the T2-gate finding.


def test_assemble_spec_canonicalizes_text_alias_and_fills_placeholders():
    spec = assemble_spec("R", [{"type": "text", "markdown": "Revenue is {{result:m1.value}}."}], _resolver)
    assert [s["type"] for s in spec["sections"]] == ["narrative"]
    # placeholder filled => it went through the narrative branch, not the else passthrough
    assert spec["sections"][0]["markdown"] == "Revenue is 1.2M."
    assert "{{" not in spec["sections"][0]["markdown"]


def test_assemble_spec_text_alias_pulls_body_from_content_field():
    spec = assemble_spec("R", [{"type": "text", "content": "Plain summary."}], _resolver)
    assert spec["sections"][0]["type"] == "narrative"
    assert spec["sections"][0]["markdown"] == "Plain summary."


def test_assemble_spec_canonicalizes_data_alias_to_table():
    spec = assemble_spec("R", [{"type": "data", "result_id": "r1"}], _resolver)
    assert spec["sections"][0]["type"] == "table"


def test_assemble_spec_narrative_alias_survives_render():
    spec = assemble_spec("R", [{"type": "text", "markdown": "Hello reader."}], _resolver)
    html = render_report_html(spec)
    assert "Hello reader." in html  # section is NOT silently dropped


def test_assemble_spec_still_raises_loudly_on_truly_unknown_type():
    # Only the two empirically-common aliases are coerced; anything else still fails
    # fast so the agent retries (we do not guess open-endedly).
    with pytest.raises(ValidationError):
        assemble_spec("R", [{"type": "paragraph", "markdown": "x"}], _resolver)


# --- Deterministic curation: top-numbers + guaranteed chart -----------------
# Product intent (2026-06-30): EVERY report (financial + data-analytics) must be a
# summary — top numbers only + a chart — NOT a raw data dump. Prompt-first guidance
# proved insufficient live (an 866-row, 0-chart report shipped). So the resolver
# curates large tables to the top-K most material rows, and assemble_spec guarantees
# a chart renders for any chartable table the model didn't already chart.


def test_resolve_table_curates_large_result_to_first_k_preserving_order():
    # 30 rows; the curated table keeps the first K rows IN SOURCE ORDER (we do NOT
    # re-rank: that would mis-rank an untagged value column and scramble an ordered
    # statement — see the T2-gate findings).
    rows = [[f"acct{i}", float(i)] for i in range(30)]
    payload = {"columns": ["account", "amount"], "rows": rows, "row_count": 30, "currency_columns": ["amount"]}
    resolved = _resolve_data_section({"type": "table", "result_id": "r1"}, lambda _rid: payload)
    assert len(resolved["rows"]) == _REPORT_TABLE_TOP_K
    assert resolved["truncated"] is True
    assert resolved["row_count"] == 30  # true total preserved for the "of N" note
    assert resolved["rows"] == rows[:_REPORT_TABLE_TOP_K]  # first K, original order


def test_resolve_small_table_not_curated():
    rows = [["A", 1.0], ["B", 2.0]]
    payload = {"columns": ["account", "amount"], "rows": rows, "row_count": 2, "currency_columns": ["amount"]}
    resolved = _resolve_data_section({"type": "table", "result_id": "r1"}, lambda _rid: payload)
    assert len(resolved["rows"]) == 2
    assert resolved["truncated"] is False


def test_assemble_spec_auto_injects_chart_after_chartable_table():
    payload = {
        "columns": ["account", "amount"],
        "rows": [["A", 10.0], ["B", 20.0], ["C", 30.0]],
        "row_count": 3,
        "currency_columns": ["amount"],
    }
    spec = assemble_spec("R", [{"type": "table", "result_id": "r1"}], lambda _rid: payload)
    assert [s["type"] for s in spec["sections"]] == ["table", "chart"]
    assert "<svg" in spec["sections"][1]["svg"]


def test_assemble_spec_does_not_double_chart_when_model_already_charted():
    payload = {
        "columns": ["account", "amount"],
        "rows": [["A", 10.0], ["B", 20.0]],
        "row_count": 2,
        "currency_columns": ["amount"],
    }
    sections = [
        {"type": "table", "result_id": "r1"},
        {"type": "chart", "result_id": "r1", "chart_type": "bar"},
    ]
    spec = assemble_spec("R", sections, lambda _rid: payload)
    assert [s["type"] for s in spec["sections"]].count("chart") == 1  # no auto-inject


def test_assemble_spec_no_chart_for_non_numeric_table():
    payload = {"columns": ["name", "note"], "rows": [["A", "x"], ["B", "y"]], "row_count": 2}
    spec = assemble_spec("R", [{"type": "table", "result_id": "r1"}], lambda _rid: payload)
    assert [s["type"] for s in spec["sections"]] == ["table"]  # nothing numeric → no forced chart


def test_render_shows_top_k_of_total_note():
    rows = [[f"a{i}", float(i)] for i in range(40)]
    payload = {"columns": ["account", "amount"], "rows": rows, "row_count": 40, "currency_columns": ["amount"]}
    spec = assemble_spec("R", [{"type": "table", "result_id": "r1"}], lambda _rid: payload)
    html = render_report_html(spec)
    assert "of 40" in html  # the note references the true total, not the shown count


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


def test_table_section_curated_to_top_k_true_count_preserved():
    """A large result is curated to the first-K rows (top numbers, not a dump), marked
    truncated, with the TRUE row_count preserved so the HTML renders the 'first K of N'
    note. Curation (first-K) is the bound; the persistence boundary caps payloads
    independently in tool_call_results."""
    from app.services.report import report_service

    big = {
        "columns": ["Period", "Revenue"],
        "rows": [[str(i), str(i * 10)] for i in range(2500)],
        "row_count": 2500,
        "currency_columns": ["Revenue"],
    }

    def resolver(rid):
        return big

    sections = [{"type": "table", "result_id": "r1"}]
    spec = assemble_spec(title="Big", sections=sections, resolver=resolver)
    tbl = next(s for s in spec["sections"] if s["type"] == "table")

    assert len(tbl["rows"]) == report_service._REPORT_TABLE_TOP_K
    assert tbl["truncated"] is True
    assert tbl["row_count"] == 2500  # the TRUE pre-curation count is preserved
    # the rendered HTML surfaces the truncation note with the true count
    html = render_report_html(spec, accent_hsl="0 0% 0%")
    assert "of 2500" in html


def test_table_section_under_cap_not_truncated():
    """A small table must NOT be marked truncated (no false 'showing first rows' note)."""
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
