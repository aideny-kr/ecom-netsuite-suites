from __future__ import annotations

import re
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import set_tenant_context
from app.schemas.chart import ChartData
from app.schemas.report import normalize_and_validate_sections
from app.services import audit_service
from app.services.chat.tool_call_results import MAX_STORED_PAYLOAD_ROWS
from app.services.report.report_charts import render_chart_svg
from app.services.report.report_html import render_report_html

_PLACEHOLDER = re.compile(r"\{\{(result|metric):([^}]+)\}\}")
_METRIC_COLUMNS = ["Metric", "Value", "Unit", "Period"]
# Cap rendered table rows: report.compose resolves the FULL uncapped payload (a SuiteQL
# result can be up to NETSUITE_SUITEQL_MAX_ROWS = 50k), and every row lands verbatim in
# the JSONB spec + the HTML <table> the viewer iframe must render. Without a cap a single
# large result bakes multi-MB JSONB + HTML into one row (risking the Supabase 2-min INSERT
# timeout) and freezes the browser. We keep the TRUE row_count + mark truncated so
# render_report_html shows the "Showing first rows of N" note.
#
# This is the SAME constant the persistence boundary uses (MAX_STORED_PAYLOAD_ROWS in
# tool_call_results) — imported, NOT redefined, so the stored-payload cap and the render
# cap can never drift (re-gate r3, finding #6). The stored payload is already capped at
# this value, so this render-time cap is now a defense-in-depth backstop.
_MAX_REPORT_TABLE_ROWS = MAX_STORED_PAYLOAD_ROWS
# Cap chart points: unlike the table branch, the chart branch emits 2+ SVG nodes per row
# per series, so a 50k-row payload bakes a multi-MB SVG into the JSONB spec + rendered_html
# (the DoS-shape the table cap guards against). A chart is also illegible past a few dozen
# categories. Refuse deterministically and tell the model to aggregate first, rather than
# silently truncating (a truncated chart misrepresents the data with no signal).
_MAX_CHART_POINTS = 100
# Probe at most this many rows when deciding which columns are chartable y-axes — a column
# qualifies if ANY non-null cell in this window parses as a number (column-wide, not row[0]).
_CHART_NUMERIC_PROBE_ROWS = 50
# A report presents the TOP-K most material rows ("top numbers only"), never the raw
# detail dump. Product intent (2026-06-30): every report — financial AND data-analytics —
# is a summary + chart, not a long table. Curation ranks by |primary value| magnitude so
# the biggest drivers survive (generic; no statement-specific logic). The model will not
# reliably curate from prompt guidance alone (proven live), so the resolver enforces it.
_REPORT_TABLE_TOP_K = 12
# A table needs at least this many rows to be worth auto-charting.
_MIN_AUTO_CHART_ROWS = 2
Resolver = Callable[[str], dict]


def _metric_fields(payload: dict) -> dict | None:
    """Detect the blessed-metric data_table shape and return its flattened fields.

    A blessed metric (``metric_compute.metric_data_table`` → ``extract_result_payload``
    Path 1) is a single-row table ``columns=['Metric','Value','Unit','Period']`` with
    NO top-level value/unit/period — the number lives in ``rows[0]``. This lifts the
    row back to the {label,value,unit,period,definition_version} the report headline +
    ``{{metric:id}}`` placeholders expect. Returns None for any non-metric payload so
    the caller can fall back to top-level reads (the hand-rolled unit-test stub shape).
    """
    if not isinstance(payload, dict):
        return None
    cols = payload.get("columns")
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], (list, tuple)):
        return None
    is_metric_cols = cols == _METRIC_COLUMNS
    # source_kind is the metric trust-boundary marker (set by extract_result_payload
    # only for suppress_llm_value metric payloads) — accept it as a secondary signal.
    if not is_metric_cols and "source_kind" not in payload:
        return None
    row = rows[0]
    if len(row) < 4:
        return None
    return {
        "label": row[0],
        "value": row[1],
        "unit": row[2],
        "period": row[3],
        "definition_version": payload.get("definition_version"),
    }


def _coerce_number(value) -> float | None:
    """Return value as a float, stripping $, %, and thousands commas, or None if
    non-numeric. Used to decide which table columns are chartable y-axes."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = value.strip().replace("$", "").replace("%", "").replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _primary_value_index(cols: list, rows: list, currency_columns: list) -> int | None:
    """The column to rank table rows by: the first currency-tagged column, else the
    first column that probes numeric (skipping col 0, the label/x-axis), else col 0 if
    it is itself numeric. None when nothing is numeric."""
    for c in currency_columns:
        if c in cols:
            return cols.index(c)
    probe = rows[:_CHART_NUMERIC_PROBE_ROWS]
    for i in range(1, len(cols)):
        if any(i < len(r) and _coerce_number(r[i]) is not None for r in probe):
            return i
    if cols and any(r and _coerce_number(r[0]) is not None for r in probe):
        return 0
    return None


def _curate_table_rows(cols: list, rows: list, currency_columns: list, k: int) -> tuple[list, bool]:
    """Keep the top-K most material rows by |primary value| magnitude ("top numbers").

    Returns ``(rows, curated)``. A result at or under K is returned unchanged
    (curated=False). With no numeric basis to rank by, falls back to the first K rows.
    """
    if len(rows) <= k:
        return rows, False
    vidx = _primary_value_index(cols, rows, currency_columns)
    if vidx is None:
        return rows[:k], True

    def _mag(r):
        n = _coerce_number(r[vidx]) if vidx < len(r) else None
        return abs(n) if n is not None else float("-inf")

    return sorted(rows, key=_mag, reverse=True)[:k], True


def _build_tabular_chart(cols: list, rows: list, *, chart_type: str | None, title: str | None) -> "ChartData | None":
    """Build ChartData from a tabular payload: first column = x-axis, every column that
    probes numeric = a y-axis series. Coerces charted cells ($,%,commas → float; a
    non-parsing cell → 0.0). Returns None when nothing is chartable. Shared by the
    explicit `chart` section branch and the auto-chart injector."""
    if not cols or not rows:
        return None
    probe = rows[:_CHART_NUMERIC_PROBE_ROWS]
    numeric_cols = [
        c
        for i, c in enumerate(cols[1:], start=1)
        if any(i < len(r) and _coerce_number(r[i]) is not None for r in probe)
    ]
    if not numeric_cols:
        return None
    numeric_set = set(numeric_cols)

    def _row_dict(r):
        d = dict(zip(cols, r))
        for c in numeric_set:
            d[c] = _coerce_number(d.get(c)) or 0.0
        return d

    return ChartData(
        chart_type=chart_type or "bar",
        title=title or f"{numeric_cols[0]} by {cols[0]}",
        x_axis={"label": cols[0], "key": cols[0]},
        y_axes=[{"label": c, "key": c} for c in numeric_cols],
        data=[_row_dict(r) for r in rows],
    )


def _auto_chart_section(resolved: dict) -> dict | None:
    """A bar chart of a resolved table's (already top-K-curated) rows, so every
    data-heavy report visualizes its drivers even when the model composed no chart.
    None when the table is too small or has nothing numeric to plot."""
    if resolved.get("type") != "table":
        return None
    rows = resolved.get("rows", [])
    if len(rows) < _MIN_AUTO_CHART_ROWS:
        return None
    chart = _build_tabular_chart(resolved.get("columns", []), rows, chart_type="bar", title=None)
    if chart is None:
        return None
    return {"type": "chart", "svg": render_chart_svg(chart), "chart_type": chart.chart_type}


def fill_placeholders(text: str, resolver: Resolver) -> str:
    def _sub(m: re.Match) -> str:
        kind, ref = m.group(1), m.group(2).strip()
        rid, _, field = ref.partition(".")
        try:
            payload = resolver(rid)
        except Exception:
            return f"[unresolved: {kind}:{ref}]"
        if kind == "metric":
            field = field or "value"
            metric = _metric_fields(payload)
            if metric is not None:
                val = metric.get(field) if field in metric else metric.get("value")
                return str(val) if val is not None else f"[unresolved: {kind}:{ref}]"
        val = payload.get(field) if field else payload.get("value")
        return str(val) if val is not None else f"[unresolved: {kind}:{ref}]"

    return _PLACEHOLDER.sub(_sub, text)


def _resolve_data_section(s: dict, resolver: Resolver) -> dict:
    try:
        payload = resolver(s["result_id"])
    except Exception as exc:
        return {"type": "error", "reason": f"{s.get('result_id')}: {exc}"}
    if s["type"] == "table":
        cols, rows = payload.get("columns", []), payload.get("rows", [])
        if s.get("select"):
            idx = [cols.index(c) for c in s["select"] if c in cols]
            cols = [cols[i] for i in idx]
            rows = [[r[i] for i in idx] for r in rows]
        # The TRUE pre-curation count drives the "top K of N" note: prefer the upstream
        # tool's reported row_count, else the resolved row length.
        true_row_count = payload.get("row_count", len(rows))
        upstream_truncated = bool(payload.get("truncated", False))
        # Storage anti-bloat hard cap (defense-in-depth; the payload is already capped).
        if len(rows) > _MAX_REPORT_TABLE_ROWS:
            rows = rows[:_MAX_REPORT_TABLE_ROWS]
        # Carry the producer's currency-column tags so the renderer accounting-formats
        # only those columns; narrow to the columns that survived `select`.
        currency_columns = [c for c in (payload.get("currency_columns") or []) if c in cols]
        # Curate to the TOP-K most material rows — a report shows top numbers, not a dump.
        rows, curated = _curate_table_rows(cols, rows, currency_columns, _REPORT_TABLE_TOP_K)
        return {
            "type": "table",
            "columns": cols,
            "rows": rows,
            "row_count": true_row_count,
            "truncated": upstream_truncated or curated,
            "currency_columns": currency_columns,
        }
    if s["type"] == "metric_headline":
        # Prefer the real blessed-metric row shape; fall back to top-level reads so the
        # hand-rolled unit-test stub ({value,unit,period} keys) still resolves.
        metric = _metric_fields(payload)
        if metric is not None:
            return {
                "type": "metric_headline",
                "label": s.get("label") or metric.get("label") or "",
                "value": metric.get("value", ""),
                "unit": metric.get("unit", ""),
                "period": metric.get("period", ""),
                "definition_version": metric.get("definition_version"),
            }
        return {
            "type": "metric_headline",
            "label": s.get("label") or payload.get("display_name", ""),
            "value": payload.get("value", ""),
            "unit": payload.get("unit", ""),
            "period": payload.get("period", ""),
            "definition_version": payload.get("definition_version"),
        }
    if s["type"] == "chart":
        cd = payload.get("chart_data")
        if cd is None:  # build a minimal ChartData from a tabular payload
            # A single-number blessed-metric result is headline material, not a chart:
            # its only numeric column is 'Value' (Unit/Period would float()->0.0 nonsense
            # bars). Refuse deterministically rather than persist a misleading frozen chart.
            if _metric_fields(payload) is not None:
                return {"type": "error", "reason": "metric results are headline material, not charts"}
            cols = payload.get("columns", [])
            rows = payload.get("rows", [])
            # Row cap: a chart over tens of thousands of rows bakes a multi-MB SVG into
            # the report (DoS-shape). Refuse deterministically before building anything.
            if len(rows) > _MAX_CHART_POINTS:
                return {
                    "type": "error",
                    "reason": f"too many rows to chart ({len(rows)} > {_MAX_CHART_POINTS}) — aggregate first",
                }
            chart = _build_tabular_chart(cols, rows, chart_type=s.get("chart_type"), title=s.get("label") or "Chart")
            if chart is None:
                return {"type": "error", "reason": "no numeric columns to chart"}
        else:
            chart = ChartData.model_validate(cd)
            if s.get("chart_type"):
                chart.chart_type = s["chart_type"]
        return {"type": "chart", "svg": render_chart_svg(chart), "chart_type": chart.chart_type}
    return s


def assemble_spec(title: str, sections: list[dict], resolver: Resolver) -> dict:
    # Canonicalize the LLM's section-type aliases (text->narrative, data->table) HERE,
    # before we read s["type"], and validate in the same pass. The chat tool path
    # (report_export.execute -> compose_report -> assemble_spec) passes the raw LLM
    # dicts and never constructs ComposeRequest, so this is the only boundary that
    # reliably runs on the real path. Without it, a `text` section would fall through
    # to the heading/divider else-branch below and be dropped SILENTLY by the renderer.
    sections = normalize_and_validate_sections(sections)  # raises loudly on a truly-unknown type
    # result_ids the model already charted — don't auto-add a redundant second chart.
    charted_ids = {s.get("result_id") for s in sections if s.get("type") == "chart"}
    provenance_sources: list[str] = []
    out_sections: list[dict] = []
    for s in sections:
        t = s["type"]
        if t == "narrative":
            out_sections.append({"type": "narrative", "markdown": fill_placeholders(s["markdown"], resolver)})
        elif t in ("table", "metric_headline", "chart"):
            resolved = _resolve_data_section(s, resolver)
            out_sections.append(resolved)
            if resolved.get("type") == "metric_headline" and resolved.get("definition_version") is not None:
                provenance_sources.append(f"metric:{s['result_id']}@v{resolved['definition_version']}")
            # Guarantee a chart: a report visualizes its drivers. If the model composed a
            # `table` but no chart for the same result, auto-append a bar chart of the
            # curated top-K rows — prompt guidance alone does not reliably produce one.
            if t == "table" and s.get("result_id") not in charted_ids:
                auto = _auto_chart_section(resolved)
                if auto is not None:
                    out_sections.append(auto)
                    charted_ids.add(s.get("result_id"))
        else:  # heading / divider
            out_sections.append(s)
    return {"title": title, "sections": out_sections, "provenance": {"sources": provenance_sources}}


async def compose_report(
    db: AsyncSession,
    *,
    tenant_id,
    title: str,
    sections: list[dict],
    resolver: Resolver,
    created_by=None,
    source_run_id=None,
    accent_hsl: str = "240 6% 10%",
) -> dict:
    from app.models.report import Report

    spec = assemble_spec(title, sections, resolver)
    html = render_report_html(spec, accent_hsl=accent_hsl)
    await set_tenant_context(db, str(tenant_id))  # TOOL path: RLS context not pre-set
    report = Report(
        tenant_id=tenant_id,
        title=title,
        spec_json=spec,
        rendered_html=html,
        created_by=created_by,
        source_run_id=source_run_id,
    )
    db.add(report)
    await db.flush()
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="report",
        action="report.compose",
        actor_id=created_by,
        resource_type="report",
        resource_id=str(report.id),
    )
    # TURN ATOMICITY (gate cluster A): do NOT commit here. compose_report runs as a
    # chat tool on the orchestrator's SHARED in-turn session; the orchestrator
    # commits exactly ONCE at end of turn, so the report row + its audit persist
    # atomically with the turn. A mid-turn commit would survive even if a later
    # step rolls back, orphaning a committed report under a failed turn. We flush
    # (above) so report.id is assigned for the audit row + the returned id.
    return {"report_id": str(report.id), "title": title, "section_count": len(spec["sections"])}
