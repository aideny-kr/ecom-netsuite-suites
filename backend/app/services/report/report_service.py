from __future__ import annotations

import re
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import set_tenant_context
from app.schemas.chart import ChartData
from app.schemas.report import parse_sections
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
        # The TRUE pre-cap count drives the "Showing first rows of N" note: prefer the
        # upstream tool's reported row_count, else the resolved row length.
        true_row_count = payload.get("row_count", len(rows))
        upstream_truncated = bool(payload.get("truncated", False))
        # Cap rows so a huge result doesn't bloat the JSONB spec / freeze the viewer.
        capped = len(rows) > _MAX_REPORT_TABLE_ROWS
        if capped:
            rows = rows[:_MAX_REPORT_TABLE_ROWS]
        return {
            "type": "table",
            "columns": cols,
            "rows": rows,
            "row_count": true_row_count,
            "truncated": upstream_truncated or capped,
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
            # Restrict y-axes to NUMERIC columns only, probing the WHOLE column (a window
            # of the first rows), not just rows[0]: a column that is NULL/blank in the
            # first row but numeric later (e.g. an opening period with no data yet) is a
            # valid y-axis. A column qualifies if AT LEAST ONE non-null cell in the probe
            # window parses as a number. The first column is the x-axis.
            probe = rows[:_CHART_NUMERIC_PROBE_ROWS]
            numeric_cols = [
                c
                for i, c in enumerate(cols[1:], start=1)
                if any(i < len(r) and _coerce_number(r[i]) is not None for r in probe)
            ]
            if not numeric_cols:
                return {"type": "error", "reason": "no numeric columns to chart"}
            numeric_set = set(numeric_cols)

            def _row_dict(r):
                d = dict(zip(cols, r))
                # The column already qualified as numeric. Coerce each charted cell
                # (strip $,%,commas) so the renderer plots real bars instead of
                # float('$1,000')->0.0 flat bars; a non-parsing cell (e.g. a NULL in an
                # otherwise-numeric column) coerces to 0.0 — a real zero-height bar.
                for c in numeric_set:
                    d[c] = _coerce_number(d.get(c)) or 0.0
                return d

            chart = ChartData(
                chart_type=s.get("chart_type") or "bar",
                title=s.get("label") or "Chart",
                x_axis={"label": cols[0] if cols else "x", "key": cols[0] if cols else "x"},
                y_axes=[{"label": c, "key": c} for c in numeric_cols],
                data=[_row_dict(r) for r in rows],
            )
        else:
            chart = ChartData.model_validate(cd)
            if s.get("chart_type"):
                chart.chart_type = s["chart_type"]
        return {"type": "chart", "svg": render_chart_svg(chart), "chart_type": chart.chart_type}
    return s


def assemble_spec(title: str, sections: list[dict], resolver: Resolver) -> dict:
    parse_sections(sections)  # validates shape; raises on unknown type
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
