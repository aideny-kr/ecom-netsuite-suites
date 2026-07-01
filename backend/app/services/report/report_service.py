from __future__ import annotations

import math
import re
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import set_tenant_context
from app.schemas.chart import ChartData
from app.schemas.report import normalize_and_validate_sections
from app.services import audit_service
from app.services.report.report_charts import render_chart_svg
from app.services.report.report_html import _fmt_amount, render_report_html

_PLACEHOLDER = re.compile(r"\{\{(result|metric):([^}]+)\}\}")
_METRIC_COLUMNS = ["Metric", "Value", "Unit", "Period"]
# Cap chart points: unlike the table branch, the chart branch emits 2+ SVG nodes per row
# per series, so a 50k-row payload bakes a multi-MB SVG into the JSONB spec + rendered_html
# (the DoS-shape the table cap guards against). A chart is also illegible past a few dozen
# categories. Refuse deterministically and tell the model to aggregate first, rather than
# silently truncating (a truncated chart misrepresents the data with no signal).
_MAX_CHART_POINTS = 100
# A report presents the first K rows ("top numbers only"), never the raw detail dump.
# Product intent (2026-06-30): every report — financial AND data-analytics — is a summary
# + chart, not a long table. Curation keeps SOURCE ORDER (a statement's line sequence /
# a query's ORDER BY) rather than re-ranking, which would scramble ordered statements and
# mis-rank when the value column can't be identified. The model will not reliably curate
# from prompt guidance alone (proven live), so the resolver enforces it.
_REPORT_TABLE_TOP_K = 12
# A table needs at least this many rows to be worth auto-charting.
_MIN_AUTO_CHART_ROWS = 2
# Statement treatment (product shape confirmed 2026-07-01: "callouts + statement"): a
# statement-shaped result (line_meta present — flattened ns_runReport statements) renders
# as up to _STATEMENT_CALLOUT_MAX marquee metric_headline cards + a curated table of at
# most _STATEMENT_TABLE_MAX NAMED section-summary lines. Selection is purely STRUCTURAL
# (line_meta.is_summary/level + a non-null amount) — never label matching, so blanks and
# amount-less placeholder rows drop out without any hardcoded names (no prompt
# pollution). Fewer than _MIN_STATEMENT_LINES qualifying lines ⇒ not a statement ⇒ the
# general top-K floor applies (all non-statement reports keep today's behavior).
_STATEMENT_TABLE_MAX = 8
_STATEMENT_CALLOUT_MAX = 4
_MIN_STATEMENT_LINES = 2
# Time-series detection (Phase 4): a chart whose x column is period/date-SHAPED renders
# as a LINE (a trend), not account-style bars. Matched on the VALUES' shape only — never
# on column names (no schema assumptions). Conservative: a miss just keeps the bar
# default, which is always safe.
_TIME_LIKE_RE = re.compile(
    r"^("
    r"\d{4}[-/.](0?[1-9]|1[0-2])([-/.]\d{1,2})?"  # 2026-06 / 2026-06-30
    r"|(0?[1-9]|1[0-2])[-/.]\d{4}"  # 06/2026
    r"|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[ .,'-]*\d{2,4}"  # Jun 2026
    r"|q[1-4][ .'-]?\d{2,4}"  # Q2 2026
    r"|\d{4}[ .'-]?q[1-4]"  # 2026 Q2
    r"|fy[ .'-]?\d{2,4}"  # FY26
    r")$",
    re.IGNORECASE,
)
_TIME_SERIES_MIN_FRACTION = 0.8
Resolver = Callable[[str], dict]


def _looks_time_series(x_values: list) -> bool:
    """True when the x column's VALUES are period/date-shaped (ISO dates, "Jun 2026",
    "Q2 2026", "FY26") for at least ``_TIME_SERIES_MIN_FRACTION`` of the non-empty
    cells — a trend axis, so the chart defaults to a line. Value-shape only, never
    column names."""
    vals = [str(v).strip() for v in x_values if v is not None and str(v).strip()]
    if len(vals) < _MIN_AUTO_CHART_ROWS:
        return False
    hits = sum(1 for v in vals if _TIME_LIKE_RE.match(v))
    return hits / len(vals) >= _TIME_SERIES_MIN_FRACTION


def _amount_index(cols: list, currency_columns: list) -> int:
    """Index of the amount column: the producer-tagged currency column, else col 1."""
    if currency_columns and currency_columns[0] in cols:
        return cols.index(currency_columns[0])
    return 1


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
    """Return value as a FINITE float, stripping $, %, and thousands commas, or None if
    non-numeric / non-finite. Used to decide which table columns are chartable y-axes —
    NaN/Inf (incl. the string literals float() accepts) must NOT qualify a column or bake
    a nan/inf bar into a chart."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    cleaned = value.strip().replace("$", "").replace("%", "").replace(",", "")
    if not cleaned:
        return None
    try:
        n = float(cleaned)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _numeric_value_columns(cols: list, rows: list) -> list:
    """The chartable y-axis columns: every column after col 0 (the x-axis) with at least
    one finite-numeric cell. Probes ALL rows (chart inputs are already bounded by
    _MAX_CHART_POINTS / curation), so a column that is null in the first rows but numeric
    later still qualifies. Column-wide, not row[0]."""
    return [
        c for i, c in enumerate(cols[1:], start=1) if any(i < len(r) and _coerce_number(r[i]) is not None for r in rows)
    ]


def _curate_table_rows(rows: list, k: int) -> tuple[list, bool]:
    """Curate a report table to the first K rows, PRESERVING the result's own order.

    Returns ``(rows, curated)``; a result at or under K is returned unchanged. We keep
    source order rather than re-rank by magnitude: a result is already meaningfully
    ordered (a statement's line sequence, or a query's ORDER BY), and re-ranking would
    both destroy that structure and mis-rank when the "value" column can't be identified.
    The narrative + chart carry the analysis; this just bounds the table to top numbers.
    """
    if len(rows) <= k:
        return rows, False
    return rows[:k], True


def _curate_statement(cols: list, rows: list, line_meta, currency_columns: list) -> tuple[list, list] | None:
    """Curate a statement-shaped table to its NAMED section-summary lines.

    Returns ``(statement_rows, callout_sections)`` or None when the payload is not
    statement-shaped (missing/misaligned ``line_meta``, or fewer than
    ``_MIN_STATEMENT_LINES`` qualifying lines) — the caller then falls back to the
    general top-K floor.

    A line qualifies iff it is a summary (``line_meta.is_summary``) with a non-empty
    label (col 0) AND a non-null amount — so detail GL lines, blank continuation rows,
    and amount-less placeholder/header rows all drop out STRUCTURALLY (no label
    matching). Over ``_STATEMENT_TABLE_MAX`` lines, the shallowest indent levels (the
    most aggregate subtotals) are kept, statement order always preserved. Callouts are
    the LAST ``_STATEMENT_CALLOUT_MAX`` curated lines — a statement builds to its
    conclusions (Net Change, Ending Cash) — as metric_headline sections with the amount
    accounting-formatted (exact Decimal semantics via the shared ``_fmt_amount``).
    """
    if not isinstance(line_meta, list) or len(line_meta) != len(rows) or len(cols) < 2:
        return None
    amount_idx = _amount_index(cols, currency_columns)
    picked: list[tuple[int, list]] = []
    for row, meta in zip(rows, line_meta):
        if not isinstance(meta, dict) or not meta.get("is_summary"):
            continue
        label = str(row[0]).strip() if row and row[0] is not None else ""
        amount = row[amount_idx] if amount_idx < len(row) else None
        if not label or amount is None:
            continue
        try:
            level = int(meta.get("level", 0))
        except (TypeError, ValueError):
            level = 0
        picked.append((level, row))
    if len(picked) < _MIN_STATEMENT_LINES:
        return None
    if len(picked) > _STATEMENT_TABLE_MAX:
        # Keep the shallowest levels that fit the cap (largest threshold T with
        # count(level <= T) <= cap); if even the shallowest level alone overflows,
        # keep its first cap-many lines. Statement order is preserved throughout.
        chosen = None
        for threshold in sorted({lvl for lvl, _ in picked}, reverse=True):
            subset = [p for p in picked if p[0] <= threshold]
            if len(subset) <= _STATEMENT_TABLE_MAX:
                chosen = subset
                break
        if chosen is None:
            min_level = min(lvl for lvl, _ in picked)
            chosen = [p for p in picked if p[0] == min_level][:_STATEMENT_TABLE_MAX]
        picked = chosen
    statement_rows = [row for _, row in picked]
    callouts = [
        {
            "type": "metric_headline",
            "label": str(row[0]),
            "value": _fmt_amount(row[amount_idx] if amount_idx < len(row) else None),
            "unit": "",
            "period": "",
            "definition_version": None,
        }
        for row in statement_rows[-_STATEMENT_CALLOUT_MAX:]
    ]
    return statement_rows, callouts


def _driver_rows(rows: list, line_meta: list, cols: list, currency_columns: list) -> list:
    """The top-``_REPORT_TABLE_TOP_K`` DETAIL (leaf) rows by ``|amount|``, original
    statement order preserved — the comparable drivers for a statement chart.
    Summary/subtotal/grand-total lines are EXCLUDED: charting a subtotal next to its own
    detail lines double-counts, and a grand-total bar dwarfs every real driver (the live
    bar-soup symptom). Structural (line_meta + amounts), never by label."""
    amount_idx = _amount_index(cols, currency_columns)
    leaves: list[tuple[float, int, list]] = []
    for i, (row, meta) in enumerate(zip(rows, line_meta)):
        if not isinstance(meta, dict) or meta.get("is_summary"):
            continue
        label = str(row[0]).strip() if row and row[0] is not None else ""
        amount = _coerce_number(row[amount_idx]) if amount_idx < len(row) else None
        if not label or amount is None:
            continue
        leaves.append((abs(amount), i, row))
    top = sorted(leaves, key=lambda t: t[0], reverse=True)[:_REPORT_TABLE_TOP_K]
    return [row for _, _, row in sorted(top, key=lambda t: t[1])]


def _build_tabular_chart(
    cols: list, rows: list, *, chart_type: str | None, title: str | None, value_columns: list | None = None
) -> "ChartData | None":
    """Build ChartData from a tabular payload: first column = x-axis, numeric columns =
    y-axis series. When ``value_columns`` is given the y-axes are restricted to those
    (intersected with the numeric columns) — used by the auto-chart to plot ONLY the
    money columns, never a numeric dimension (year/id/count). Coerces charted cells
    ($,%,commas → float; a non-parsing cell → 0.0). Returns None when nothing is
    chartable. Shared by the explicit `chart` section branch and the auto-chart injector."""
    if not cols or not rows:
        return None
    numeric_cols = _numeric_value_columns(cols, rows)
    if value_columns is not None:
        numeric_cols = [c for c in numeric_cols if c in value_columns]
    if not numeric_cols:
        return None
    numeric_set = set(numeric_cols)

    def _row_dict(r):
        d = dict(zip(cols, r))
        for c in numeric_set:
            d[c] = _coerce_number(d.get(c)) or 0.0
        return d

    # Shape-driven defaults (Phase 4): a period/date-shaped x column is a TREND — default
    # to a line and say "trend" in the derived title. An explicit chart_type always wins.
    time_series = _looks_time_series([r[0] if r else None for r in rows])
    if title is None:
        title = f"{numeric_cols[0]} trend by {cols[0]}" if time_series else f"{numeric_cols[0]} by {cols[0]}"
    return ChartData(
        chart_type=chart_type or ("line" if time_series else "bar"),
        title=title,
        x_axis={"label": cols[0], "key": cols[0]},
        y_axes=[{"label": c, "key": c} for c in numeric_cols],
        data=[_row_dict(r) for r in rows],
    )


def _auto_chart_section(resolved: dict, *, drivers: list | None = None, label: str | None = None) -> dict | None:
    """A chart of a resolved table's rows, so every data-heavy report visualizes its
    drivers even when the model composed no chart.

    ``drivers`` (statement tables): chart the top LEAF detail rows instead of the
    curated summary lines — a bar of comparable drivers, never subtotal/grand-total
    soup. Otherwise chart the table's own (already curated) rows, with the chart TYPE
    picked by the x column's shape (period/date-like → line trend). ``label`` (the
    model-supplied section title) titles the chart; without it a descriptive title is
    derived deterministically.

    Charts ONLY the producer-tagged currency columns (the real measures). With no tag,
    charts a SINGLE numeric column (unambiguous), but skips a multi-numeric untagged
    table — guessing which numeric column is the measure risks plotting a dimension
    (year/id/count) as a misleading series. None when too small / nothing safe to plot."""
    if resolved.get("type") != "table":
        return None
    rows = drivers if drivers else resolved.get("rows", [])
    cols = resolved.get("columns", [])
    # Curation bounds this to K (<<100), but guard the DoS-shape independently so a future
    # higher _REPORT_TABLE_TOP_K can't bake a multi-MB SVG (same cap the chart branch uses).
    if not (_MIN_AUTO_CHART_ROWS <= len(rows) <= _MAX_CHART_POINTS):
        return None
    numeric_cols = _numeric_value_columns(cols, rows)
    currency = [c for c in (resolved.get("currency_columns") or []) if c in numeric_cols]
    if currency:
        value_columns = currency
    elif len(numeric_cols) == 1:
        value_columns = numeric_cols
    else:
        return None  # ambiguous (or no) numeric measure → don't auto-chart a wrong series
    if drivers:
        title = label or f"Top {len(rows)} drivers by {value_columns[0]}"
        chart_type = "bar"  # leaf drivers are categorical by construction
    else:
        title = label  # None → _build_tabular_chart derives a trend-aware title
        chart_type = None  # let the x column's shape decide (time-series → line)
    chart = _build_tabular_chart(cols, rows, chart_type=chart_type, title=title, value_columns=value_columns)
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
        # The TRUE pre-curation count drives the "first K of N" note: prefer the upstream
        # tool's reported row_count, else the resolved row length.
        true_row_count = payload.get("row_count", len(rows))
        upstream_truncated = bool(payload.get("truncated", False))
        # Carry the producer's currency-column tags so the renderer accounting-formats
        # only those columns; narrow to the columns that survived `select`.
        currency_columns = [c for c in (payload.get("currency_columns") or []) if c in cols]
        # STATEMENT treatment (Phase 3): a statement-shaped payload (line_meta present —
        # flattened ns_runReport statements) curates to its named section-summary lines +
        # marquee callouts instead of the positional top-K. Gated on no `select`: a
        # model projection re-indexes columns, and the conservative rule there is the
        # plain top-K floor rather than risking curation off the wrong column.
        if not s.get("select"):
            curated_stmt = _curate_statement(cols, rows, payload.get("line_meta"), currency_columns)
            if curated_stmt is not None:
                statement_rows, callouts = curated_stmt
                return {
                    "type": "table",
                    "columns": cols,
                    "rows": statement_rows,
                    "row_count": true_row_count,
                    "truncated": True,  # fewer lines shown than the source statement
                    "curation": "statement",
                    "currency_columns": currency_columns,
                    # Internal hand-offs to assemble_spec (consumed there, then
                    # stripped) — never persisted into the frozen spec. Callouts render
                    # ABOVE the table; drivers feed the auto-chart with the top LEAF
                    # detail rows (the statement table shows the summaries, the chart
                    # shows the movers — never subtotal+detail double-count).
                    "statement_callouts": callouts,
                    "statement_drivers": _driver_rows(rows, payload.get("line_meta"), cols, currency_columns),
                }
        # Curate to the first K rows — a report shows top numbers, not a dump. This also
        # bounds the rendered table (no separate anti-bloat cap needed: K << any payload).
        rows, curated = _curate_table_rows(rows, _REPORT_TABLE_TOP_K)
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
            # An explicit chart over a STATEMENT payload (line_meta aligned) charts the
            # top LEAF detail drivers — a subtotal/grand-total bar next to its own
            # details double-counts and dwarfs the real movers (the live bar-soup).
            default_title = None
            line_meta = payload.get("line_meta")
            if isinstance(line_meta, list) and len(line_meta) == len(rows):
                stmt_currency = [c for c in (payload.get("currency_columns") or []) if c in cols]
                drivers = _driver_rows(rows, line_meta, cols, stmt_currency)
                if len(drivers) >= _MIN_AUTO_CHART_ROWS:
                    rows = drivers
                    value_name = stmt_currency[0] if stmt_currency else (cols[1] if len(cols) > 1 else "value")
                    default_title = f"Top {len(rows)} drivers by {value_name}"
            # Row cap: a chart over tens of thousands of rows bakes a multi-MB SVG into
            # the report (DoS-shape). Refuse deterministically before building anything.
            if len(rows) > _MAX_CHART_POINTS:
                return {
                    "type": "error",
                    "reason": f"too many rows to chart ({len(rows)} > {_MAX_CHART_POINTS}) — aggregate first",
                }
            # Title: the model's label wins; else the driver title; else _build_tabular_chart
            # derives a descriptive one from the data (never the junk "Chart" default).
            chart = _build_tabular_chart(
                cols, rows, chart_type=s.get("chart_type"), title=s.get("label") or default_title
            )
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
    # Pass 1: resolve every section up front so chart success is known before we decide
    # which tables need an auto-chart (ordering-independent; a chart may precede its table).
    resolved_pairs: list[tuple[dict, dict]] = []
    for s in sections:
        if s["type"] == "narrative":
            resolved_pairs.append((s, {"type": "narrative", "markdown": fill_placeholders(s["markdown"], resolver)}))
        elif s["type"] in ("table", "metric_headline", "chart"):
            resolved_pairs.append((s, _resolve_data_section(s, resolver)))
        else:  # heading / divider
            resolved_pairs.append((s, s))
    # result_ids the model SUCCESSFULLY charted itself — an explicit chart that resolved to
    # an error must NOT suppress the table's auto-chart fallback.
    model_charted_ids = {
        s.get("result_id") for s, r in resolved_pairs if s["type"] == "chart" and r.get("type") == "chart"
    }
    # Auto-chart dedupe is keyed by (result_id, select) so two tables over the SAME result
    # with DIFFERENT projections each get their own chart, but an identical table doesn't
    # double-chart.
    auto_charted_keys: set = set()

    def _table_key(sec: dict) -> tuple:
        return (sec.get("result_id"), tuple(sec.get("select") or []))

    # Pass 2: emit, auto-appending a chart after any chartable table the model did not
    # successfully chart — a report visualizes its drivers; prompt guidance alone does not.
    provenance_sources: list[str] = []
    out_sections: list[dict] = []
    for s, resolved in resolved_pairs:
        # Statement treatment: marquee callout cards render ABOVE their curated
        # statement table; leaf DRIVERS feed the auto-chart. Pop both internal
        # hand-off keys so they never persist into the frozen spec_json.
        callouts = resolved.pop("statement_callouts", None) if isinstance(resolved, dict) else None
        drivers = resolved.pop("statement_drivers", None) if isinstance(resolved, dict) else None
        if callouts:
            out_sections.extend(callouts)
        out_sections.append(resolved)
        if resolved.get("type") == "metric_headline" and resolved.get("definition_version") is not None:
            provenance_sources.append(f"metric:{s['result_id']}@v{resolved['definition_version']}")
        if (
            s["type"] == "table"
            and s.get("result_id") not in model_charted_ids
            and _table_key(s) not in auto_charted_keys
        ):
            auto = _auto_chart_section(resolved, drivers=drivers, label=s.get("label"))
            if auto is not None:
                out_sections.append(auto)
                auto_charted_keys.add(_table_key(s))
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
