# backend/app/services/metrics/metric_compute.py
"""Deterministic execution of a metric: coerce params, fill blessed query, execute, shape as data_table."""

import re
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric_definition import MetricDefinition
from app.services.metrics.expression_evaluator import evaluate_expression
from app.services.metrics.metric_resolver import resolve_metrics
from app.services.metrics.period_resolver import resolve_period


class ParamError(ValueError):
    pass


def coerce_params(
    params_schema: dict,
    params: dict,
    *,
    fiscal_year_start_month: int = 1,
    today: date | None = None,
) -> dict:
    schema = params_schema or {}
    for name in params:
        if name not in schema:
            raise ParamError(f"unknown param: {name}")
    out: dict = {}
    for name, spec in schema.items():
        ptype = spec.get("type")
        if ptype == "period":
            token = params.get(name, "this_month")
            s, e = resolve_period(
                token,
                fiscal_year_start_month=fiscal_year_start_month,
                today=today or date.today(),
            )
            out["period_start"] = s.isoformat()
            out["period_end"] = e.isoformat()
            continue
        if name not in params:
            raise ParamError(f"missing param: {name}")
        val = params[name]
        if ptype == "int":
            try:
                out[name] = int(val)
            except (TypeError, ValueError) as ex:
                raise ParamError(f"{name} must be int") from ex
        elif ptype == "date":
            try:
                out[name] = datetime.strptime(str(val), "%Y-%m-%d").date().isoformat()
            except ValueError as ex:
                raise ParamError(f"{name} must be YYYY-MM-DD") from ex
        elif ptype == "enum":
            if val not in spec.get("values", []):
                raise ParamError(f"{name} not in allowed values")
            out[name] = val
        else:
            raise ParamError(f"unsupported param type: {ptype}")
    return out


def fill_query(query: str, coerced: dict) -> str:
    def _render(v) -> str:
        return str(v) if isinstance(v, int) else f"'{v}'"

    filled = query
    for name, val in coerced.items():
        filled = re.sub(rf":{re.escape(name)}\b", _render(val), filled)
    if re.search(r":[a-zA-Z_]\w*", filled):
        raise ParamError("unfilled placeholder remains")
    return filled


def metric_data_table(display_name: str, value, unit: str, period_label: str, spec) -> dict:
    return {
        "columns": ["Metric", "Value", "Unit", "Period"],
        "rows": [[display_name, value, unit, period_label]],
        "row_count": 1,
        "query": spec,
        "truncated": False,
    }


async def _execute_scalar_query(db, tenant_id, metric: MetricDefinition, coerced: dict, context: dict) -> float:
    from app.mcp.tools import netsuite_suiteql

    query = fill_query(metric.blessed_spec["query"], coerced)
    if not netsuite_suiteql.is_read_only_sql(query):
        raise ParamError("filled query failed read-only validation")
    # Execute via the existing tool path; returns the first scalar cell.
    result = await netsuite_suiteql.execute({"query": query}, {"tenant_id": str(tenant_id), "db": db})
    rows = result.get("rows") or [[0]]
    return float(rows[0][0])


async def compute_metric(db: AsyncSession, *, tenant_id, key: str, params: dict, context: dict) -> dict:
    matches = await resolve_metrics(db, tenant_id=tenant_id, query=key, top_k=1)
    metric = next((m for m in matches if m.key == key), None)
    if metric is None:
        return {
            "error": "no_blessed_definition",
            "key": key,
            "message": f"No blessed definition for '{key}'.",
        }
    fy = int(context.get("fiscal_year_start_month", 1) or 1)
    coerced = coerce_params(metric.params_schema or {}, params, fiscal_year_start_month=fy)
    period_label = params.get("period", "")

    if metric.source_kind == "expression":
        leaves = {}
        for dep in metric.depends_on or []:
            dmatch = next(
                (m for m in await resolve_metrics(db, tenant_id=tenant_id, query=dep, top_k=1) if m.key == dep),
                None,
            )
            if dmatch is None:
                return {
                    "error": "missing_dependency",
                    "key": dep,
                    "message": f"Missing leaf metric '{dep}'.",
                }
            leaves[dep] = await _execute_scalar_query(
                db,
                tenant_id,
                dmatch,
                coerce_params(dmatch.params_schema or {}, params, fiscal_year_start_month=fy),
                context,
            )
        value = evaluate_expression(metric.expression, leaves)
    else:
        value = await _execute_scalar_query(db, tenant_id, metric, coerced, context)

    return metric_data_table(
        metric.display_name, value, metric.unit, period_label, metric.blessed_spec or metric.expression
    )
