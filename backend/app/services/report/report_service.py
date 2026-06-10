from __future__ import annotations

import re
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import set_tenant_context
from app.schemas.chart import ChartData
from app.schemas.report import parse_sections
from app.services import audit_service
from app.services.report.report_charts import render_chart_svg
from app.services.report.report_html import render_report_html

_PLACEHOLDER = re.compile(r"\{\{(result|metric):([^}]+)\}\}")
Resolver = Callable[[str], dict]


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
        return {
            "type": "table",
            "columns": cols,
            "rows": rows,
            "row_count": payload.get("row_count", len(rows)),
            "truncated": payload.get("truncated", False),
        }
    if s["type"] == "metric_headline":
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
            cols = payload.get("columns", [])
            chart = ChartData(
                chart_type=s.get("chart_type") or "bar",
                title=s.get("label") or "Chart",
                x_axis={"label": cols[0] if cols else "x", "key": cols[0] if cols else "x"},
                y_axes=[{"label": c, "key": c} for c in cols[1:]] or [{"label": "value", "key": "value"}],
                data=[dict(zip(cols, r)) for r in payload.get("rows", [])],
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
