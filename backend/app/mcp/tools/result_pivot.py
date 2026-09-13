"""Reshape complete Metabase aggregates without requerying or changing sources."""

from __future__ import annotations

import copy
import json
from decimal import Decimal, localcontext
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from app.core.dependencies import has_permission
from app.models.chat import ChatSession
from app.models.mcp_connector import McpConnector
from app.models.tenant import Tenant
from app.models.user import User
from app.services.chat.metabase_evidence import _decimal, _measure_keys
from app.services.chat.metabase_tool_policy import is_read_only_metabase_tool
from app.services.chat.result_cache import get_full_payload_entry
from app.services.chat.tool_call_results import load_conversation_tool_messages
from app.services.pivot_service import _natural_sort_key


class ResultPivot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    result_id: str = Field(pattern=r"^r[1-9][0-9]{0,7}$")
    control_result_id: str | None = Field(default=None, pattern=r"^r[1-9][0-9]{0,7}$")
    row_field: str = Field(min_length=1, max_length=256)
    column_field: str = Field(min_length=1, max_length=256)
    value_field: str = Field(min_length=1, max_length=256)
    aggregation: Literal["identity"] = "identity"
    include_total: bool = False


async def _authorize(context: dict):
    db = context.get("db")
    try:
        tenant, actor, session = (UUID(str(context.get(key))) for key in ("tenant_id", "actor_id", "conversation_id"))
    except (TypeError, ValueError):
        raise ValueError("An authenticated actor and conversation are required.") from None
    if db is None:
        raise ValueError("An authenticated conversation is required.")
    owned = await db.scalar(
        select(ChatSession.id)
        .join(User, User.id == ChatSession.user_id)
        .join(Tenant, Tenant.id == ChatSession.tenant_id)
        .where(
            ChatSession.id == session,
            ChatSession.tenant_id == tenant,
            ChatSession.user_id == actor,
            User.tenant_id == tenant,
            User.actor_type == "user",
            User.is_active.is_(True),
            Tenant.is_active.is_(True),
        )
    )
    if owned is None or not await has_permission(db, actor, "connections.view"):
        raise ValueError("This actor cannot access the requested conversation's source results.")
    return db, tenant, session


async def _entry(db, tenant, session, rid: str) -> dict:
    # Check ownership BEFORE touching the session-keyed cache. Do not use the
    # 50-row preview cache or positional aliases for unbound legacy results.
    try:
        entry = get_full_payload_entry(str(session), rid)
    except Exception:
        entry = None
    if entry is None:
        messages = await load_conversation_tool_messages(db, session, tenant)
        for message in messages:
            calls = message.tool_calls
            if not isinstance(calls, list):
                continue
            for call in calls:
                if isinstance(call, dict) and call.get("result_id") == rid:
                    entry = {"payload": call.get("result_payload"), "tool": call.get("tool")}
                    break
    if not isinstance(entry, dict) or not isinstance(entry.get("payload"), dict):
        raise ValueError(
            "The result is unavailable. Run the original query on the selected connector for a fresh result_id."
        )
    payload = entry["payload"]
    source = payload.get("metabase_source")
    from app.services.chat.tools import parse_external_tool_name

    parsed = parse_external_tool_name(entry.get("tool", ""))
    if not isinstance(source, dict) or parsed is None:
        raise ValueError(
            "A bound Metabase query result is required; previews and generated tables cannot be pivot inputs."
        )
    connector_id, raw_name = parsed
    if raw_name not in {"query", "execute_query", "execute_question"} or source.get("connector_id") != str(
        connector_id
    ):
        raise ValueError("The stored result's connector identity does not match its executed tool.")
    connector = await db.scalar(
        select(McpConnector)
        .where(McpConnector.id == connector_id, McpConnector.tenant_id == tenant)
        .execution_options(populate_existing=True)
    )
    if (
        connector is None
        or not connector.is_enabled
        or connector.status != "active"
        or not is_read_only_metabase_tool(connector, raw_name)
        or connector.server_url != source.get("server_url")
    ):
        raise ValueError(
            "The original Metabase connection is unavailable or changed. Select and query an authorized source."
        )
    _validate_payload(payload)
    from app.services.policy_service import get_active_policy

    policy = await get_active_policy(db, tenant)
    if policy is not None:
        if policy.tool_allowlist and (
            "pivot_query_result" not in policy.tool_allowlist or entry["tool"] not in policy.tool_allowlist
        ):
            raise ValueError("The current policy no longer permits this source or pivot tool.")
        blocked = {str(field).casefold() for field in (policy.blocked_fields or [])}
        names = [*payload["columns"], *source.get("column_names", [])]
        if any(str(name).casefold() in blocked for name in names):
            raise ValueError(
                "This stored result contains fields blocked by the current policy. Query permitted fields again."
            )
    return payload


def _validate_payload(payload: dict) -> None:
    source = payload.get("metabase_source", {})
    columns, rows = payload.get("columns"), payload.get("rows")
    if (
        not isinstance(columns, list)
        or not columns
        or not all(isinstance(c, str) and c for c in columns)
        or len(set(columns)) != len(columns)
        or not isinstance(rows, list)
        or len(rows) > 2000
        or any(not isinstance(r, list) or len(r) != len(columns) for r in rows)
        or len(source.get("column_sources", [])) != len(columns)
    ):
        raise ValueError("The result needs unique column names and a bounded, rectangular table.")
    if (
        source.get("complete") is not True
        or payload.get("truncated") is not False
        or payload.get("row_count") != len(rows)
    ):
        raise ValueError(
            "The result is partial or capped. Query a complete aggregate on the same connector before pivoting."
        )
    query = source.get("query")
    stages = query.get("stages") if isinstance(query, dict) else None
    if (
        not isinstance(query, dict)
        or query.get("lib/type") != "mbql/query"
        or not isinstance(stages, list)
        or not stages
        or not all(isinstance(stage, dict) for stage in stages)
    ):
        raise ValueError(
            "The executed MBQL scope is unavailable. Requery the selected source with an explicit MBQL query."
        )
    # Limits in earlier stages can trim the population before aggregation.
    if any("limit" in stage for stage in stages):
        raise ValueError("Remove MBQL stage limits and return a complete aggregate before pivoting.")


def _control_query(source: dict) -> dict:
    query = copy.deepcopy(source["query"])
    for key in ("breakout", "order-by", "limit"):
        query["stages"][-1].pop(key, None)
    return query


def reshape(params: ResultPivot, source: dict, control: dict) -> dict:
    with localcontext() as decimal_context:
        decimal_context.prec = 96
        return _reshape(params, source, control)


def _reshape(params: ResultPivot, source: dict, control: dict) -> dict:
    """Preserve server-computed cell values; never sum distinct counts/averages."""
    meta, control_meta = source["metabase_source"], control["metabase_source"]
    if any(meta.get(k) != control_meta.get(k) for k in ("connector_id", "server_url")):
        raise ValueError("The control must come from the same connector and endpoint.")
    cols, rows = source["columns"], source["rows"]
    fields = [params.row_field, params.column_field, params.value_field]
    if len(set(fields)) != 3 or any(f not in cols for f in fields):
        raise ValueError("Choose distinct row, column and aggregate-value fields from the source result.")
    ri, ci, vi = (cols.index(f) for f in fields)
    kinds = meta["column_sources"]
    stage, control_stage = meta["query"]["stages"][-1], control_meta["query"]["stages"][-1]
    if (
        len(stage.get("breakout") or []) != 2
        or kinds[ri] != "breakout"
        or kinds[ci] != "breakout"
        or kinds[vi] != "aggregation"
    ):
        raise ValueError(
            "Query exactly the two pivot dimensions as breakouts and compute the measure in Metabase first."
        )
    value_columns = [i for i, kind in enumerate(kinds) if kind == "aggregation"]
    keys, operations = _measure_keys(meta["connector_id"], meta["query"])
    control_keys, _ = _measure_keys(control_meta["connector_id"], control_meta["query"])
    control_columns = [i for i, kind in enumerate(control_meta["column_sources"]) if kind == "aggregation"]
    if (
        len(keys) != len(value_columns)
        or len(control_keys) != len(control_columns)
        or control_stage.get("breakout")
        or len(control["rows"]) != 1
        or not keys
    ):
        raise ValueError("The control must be a completed, ungrouped aggregate for the same measure.")
    measure = value_columns.index(vi)
    key, operation = keys[measure], operations[measure]
    if key not in control_keys:
        raise ValueError("The control's database, filters, joins or aggregate expression differ from the pivot source.")
    supported = {"sum", "sum-where", "count", "count-where", "distinct", "avg", "min", "max"}
    if operation not in supported:
        raise ValueError("This measure cannot be validated for pivoting; use a supported server aggregate.")
    total = control["rows"][0][control_columns[control_keys.index(key)]]
    total_number = _decimal(total)
    numbers = [_decimal(row[vi]) for row in rows if row[vi] is not None]
    if any(v is None for v in numbers) or (total is not None and total_number is None):
        raise ValueError("Aggregate values must be finite numbers or null, never formatted text.")
    if any(abs(v.adjusted()) > 40 for v in numbers + ([total_number] if total_number is not None else []) if v):
        raise ValueError("The aggregate magnitude exceeds supported pivot precision.")
    additive = operation in {"sum", "sum-where", "count", "count-where"}
    if operation in {"count", "count-where", "distinct"} and (
        len(numbers) != len(rows)
        or total_number is None
        or any(v < 0 or v != v.to_integral_value() for v in [*numbers, total_number])
    ):
        raise ValueError("Counts must be nonnegative integers, including the overall control.")
    if additive and (sum(numbers, Decimal(0)) if numbers else None) != total_number:
        if not (not rows and operation in {"count", "count-where"} and total_number == 0):
            raise ValueError("The grouped values do not reconcile to the control. Requery both before pivoting.")
    if operation == "distinct":
        if total_number is None or total_number < 0 or any(v < 0 or v > total_number for v in numbers):
            raise ValueError("The distinct counts contradict their overall control.")
        if sum(numbers, Decimal(0)) < total_number:
            raise ValueError("The distinct groups do not cover the control population.")
    if operation == "min" and (min(numbers) if numbers else None) != total_number:
        raise ValueError("The minimum does not match its control.")
    if operation == "max" and (max(numbers) if numbers else None) != total_number:
        raise ValueError("The maximum does not match its control.")
    if operation == "avg" and numbers and (total_number is None or not min(numbers) <= total_number <= max(numbers)):
        raise ValueError("The average is outside the grouped averages; requery matching source and control.")
    if operation == "avg" and not numbers and total_number is not None:
        raise ValueError("An empty/all-null grouped average contradicts its non-null control.")
    if params.include_total and not additive:
        raise ValueError(
            "Row totals for distinct counts/averages/min/max need separate server aggregates. Use include_total=false."
        )

    def dimension(value):
        if isinstance(value, (dict, list, bool)):
            raise ValueError("Pivot dimensions must be scalar text, numbers, or null.")
        return json.dumps(value, ensure_ascii=False, allow_nan=False)

    labels, row_labels, cells = {}, {}, {}
    for row in rows:
        rk, ck = dimension(row[ri]), dimension(row[ci])
        if (rk, ck) in cells:
            raise ValueError(
                "Duplicate pivot cells indicate a different query grain. Aggregate the two dimensions in Metabase."
            )
        row_labels[rk] = row[ri]
        labels[ck] = "(null)" if row[ci] is None else str(row[ci])
        cells[(rk, ck)] = row[vi]
    if len(set(labels.values())) != len(labels) or set(labels.values()) & {params.row_field, "Total"}:
        raise ValueError("Pivot column labels collide. Use distinct source labels before pivoting.")
    if len(labels) > 100 or len(row_labels) * max(1, len(labels)) > 20000:
        raise ValueError("The pivot is too wide. Narrow the source dimensions or filters and query again.")
    ordered = sorted(labels, key=lambda key: _natural_sort_key(labels[key]))
    out_rows = []
    for rk, label in row_labels.items():
        values = [cells.get((rk, ck)) for ck in ordered]
        if params.include_total:
            nonnull = [_decimal(v) for v in values if v is not None]
            values.append(str(sum(nonnull, Decimal(0))) if nonnull else None)
        out_rows.append([label, *values])
    caveats = [
        "Source: Metabase. Cells retain the database-calculated measure; "
        "blank cells are absent or null groups, not verified zeros.",
        f"Overall {params.value_field} ({operation}), queried separately: "
        f"{total if total is not None else 'unavailable'}.",
    ]
    if operation == "distinct":
        caveats.append(
            "The grouped distinct counts reconcile to the separately queried overall distinct count for this result."
            if sum(numbers, Decimal(0)) == total_number
            else "The grouped distinct counts overlap and exceed the separately queried overall distinct count. "
            "Use the separate control for the overall population."
        )
    elif operation == "avg":
        caveats.append("The overall average comes from its own server aggregate, not an average of pivot cells.")
    return {
        "columns": [params.row_field, *(labels[k] for k in ordered), *(["Total"] if params.include_total else [])],
        "rows": out_rows,
        "row_count": len(out_rows),
        "truncated": False,
        "query": "",
        "pivoted": True,
        "overall_value": total,
        "source_kind": "metabase",
        "pivot_config": params.model_dump(),
        "pivot_provenance": {**meta, "result_id": params.result_id, "control_result_id": params.control_result_id},
        "caveats": caveats,
    }


async def execute(params: dict, context: dict) -> dict:
    try:
        config = ResultPivot.model_validate(params)
    except ValidationError:
        return {
            "error": (
                "Use result_id, row_field, column_field, value_field, optional control_result_id, "
                "aggregation='identity' and include_total=false. Do not mix result_id with SQL/dialect inputs."
            )
        }
    try:
        db, tenant, session = await _authorize(context)
        source = await _entry(db, tenant, session, config.result_id)
        if config.control_result_id is None:
            return {
                "error": "Run this control_query on the SAME connector, then provide its control_result_id.",
                "connector_id": source["metabase_source"]["connector_id"],
                "control_query": _control_query(source["metabase_source"]),
            }
        control = await _entry(db, tenant, session, config.control_result_id)
        return reshape(config, source, control)
    except ValueError as exc:
        return {"error": str(exc)}
