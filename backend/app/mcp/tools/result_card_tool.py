"""Present stored query results as result cards, and compare two of them.

The model supplies only labels (title, scope, column names and formats). Every
number on a card -- totals, shares, summary tiles, differences and the
comparison headline -- is computed here from the stored result payloads, so the
answer text never has to restate a tool-computed figure.

Totals follow the source aggregate: SUM and plain COUNT columns add up across
groups; a distinct count (or any other aggregate) only gets a total from an
ungrouped control result the model names, and the card says whether the rows
reconcile with that control.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_RESULT_ID = r"^r[1-9][0-9]{0,7}$"
_TOP_N_DEFAULT = 7
_TOP_N_THRESHOLD = 10
_MAX_CARD_ROWS = 500

Format = Literal["text", "integer", "number", "currency", "percent", "date"]


class ColumnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=60)
    format: Format | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class PresentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_id: str = Field(pattern=_RESULT_ID)
    title: str = Field(min_length=1, max_length=120)
    subtitle: str | None = Field(default=None, max_length=120)
    scope: str | None = Field(default=None, max_length=600)
    row_label: str | None = Field(default=None, max_length=40)
    row_label_plural: str | None = Field(default=None, max_length=40)
    columns: dict[str, ColumnSpec] = Field(default_factory=dict)
    sort_by: str | None = Field(default=None, max_length=256)
    share_of: str | None = Field(default=None, max_length=256)
    share_label: str | None = Field(default=None, max_length=60)
    control_result_id: str | None = Field(default=None, pattern=_RESULT_ID)
    totals: bool = True
    tiles: bool = False
    collapsed: bool = False
    no_total_reason: str | None = Field(default=None, max_length=120)
    top_n: int | None = Field(default=None, ge=1, le=100)


class Measure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: str = Field(min_length=1, max_length=256)
    right: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1, max_length=60)
    format: Format | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class CompareKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: str = Field(min_length=1, max_length=256)
    right: str = Field(min_length=1, max_length=256)


class CompareResults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left_result_id: str = Field(pattern=_RESULT_ID)
    right_result_id: str = Field(pattern=_RESULT_ID)
    left_label: str = Field(min_length=1, max_length=40)
    right_label: str = Field(min_length=1, max_length=40)
    key: CompareKey
    key_label: str = Field(min_length=1, max_length=40)
    key_label_plural: str = Field(min_length=1, max_length=40)
    measures: list[Measure] = Field(min_length=1, max_length=6)
    title: str = Field(min_length=1, max_length=120)
    subtitle: str | None = Field(default=None, max_length=120)
    left_control_result_id: str | None = Field(default=None, pattern=_RESULT_ID)
    right_control_result_id: str | None = Field(default=None, pattern=_RESULT_ID)
    top_n: int | None = Field(default=None, ge=1, le=100)


# ---------------------------------------------------------------------------
# Loading stored results
# ---------------------------------------------------------------------------


async def _load(context: dict, rid: str) -> dict[str, Any]:
    """Resolve ``rid`` to ``{payload, tool, as_of}`` for this actor's conversation.

    Same-turn results come from the full-payload sidecar; earlier turns from the
    persisted assistant messages, numbered exactly like
    ``resolve_payload_from_messages`` (explicit result_id first, then position).
    """
    from app.mcp.tools.result_pivot import _authorize
    from app.services.chat.result_cache import get_full_payload_entry
    from app.services.chat.tool_call_results import load_conversation_tool_messages

    db, tenant, session = await _authorize(context)
    entry = None
    try:
        sidecar = get_full_payload_entry(str(session), rid)
    except Exception:
        sidecar = None
    if isinstance(sidecar, dict) and isinstance(sidecar.get("payload"), dict):
        entry = {"payload": sidecar["payload"], "tool": sidecar.get("tool") or "", "as_of": None}
    if entry is None:
        messages = await load_conversation_tool_messages(db, session, tenant)
        positional, fallback = 0, None
        for message in messages:
            for call in message.tool_calls if isinstance(message.tool_calls, list) else []:
                if not isinstance(call, dict) or not isinstance(call.get("result_payload"), dict):
                    continue
                positional += 1
                found = {"payload": call["result_payload"], "tool": call.get("tool") or "", "as_of": message.created_at}
                if call.get("result_id") == rid:
                    entry = found
                    break
                if fallback is None and f"r{positional}" == rid:
                    fallback = found
            if entry is not None:
                break
        entry = entry or fallback
    if entry is None:
        raise ValueError(f"Result {rid} is unavailable. Run the query again for a fresh result_id.")
    payload = entry["payload"]
    if not isinstance(payload.get("columns"), list) or not isinstance(payload.get("rows"), list):
        raise ValueError(f"Result {rid} is not a table.")
    await _check_access(db, tenant, entry["tool"], payload)
    as_of = entry["as_of"] or datetime.now(UTC)
    if isinstance(as_of, datetime) and as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    return {**entry, "as_of": as_of.isoformat() if isinstance(as_of, datetime) else str(as_of)}


async def _check_access(db, tenant, tool: str, payload: dict) -> None:
    """Re-apply today's policy and connector state to a stored result before showing it."""
    from sqlalchemy import select

    from app.services.policy_service import get_active_policy

    source = payload.get("metabase_source")
    if isinstance(source, dict):
        from app.models.mcp_connector import McpConnector
        from app.services.chat.metabase_tool_policy import is_read_only_metabase_tool
        from app.services.chat.tools import parse_external_tool_name

        parsed = parse_external_tool_name(tool)
        if parsed is None or source.get("connector_id") != str(parsed[0]):
            raise ValueError("The stored result's connector identity does not match its executed tool.")
        connector = await db.scalar(
            select(McpConnector)
            .where(McpConnector.id == parsed[0], McpConnector.tenant_id == tenant)
            .execution_options(populate_existing=True)
        )
        if (
            connector is None
            or not connector.is_enabled
            or connector.status != "active"
            or not is_read_only_metabase_tool(connector, parsed[1])
            or connector.server_url != source.get("server_url")
        ):
            raise ValueError("The original Metabase connection is unavailable or changed.")
    policy = await get_active_policy(db, tenant)
    if policy is None:
        return
    if policy.tool_allowlist and tool and tool not in policy.tool_allowlist:
        raise ValueError("The current policy no longer permits this result's source.")
    blocked = {str(name).casefold() for name in (policy.blocked_fields or [])}
    names = [*payload["columns"], *((source or {}).get("column_names") or [])]
    if any(str(name).casefold() in blocked for name in names):
        raise ValueError("This result contains fields blocked by the current policy.")


# ---------------------------------------------------------------------------
# Aggregate semantics
# ---------------------------------------------------------------------------


def _split_top_level(text: str) -> list[str]:
    parts, depth, quote, start = [], 0, None, 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def _select_list(query: str) -> str | None:
    match = re.search(r"\bSELECT\b", query, re.I)
    if not match:
        return None
    depth, quote = 0, None
    body = query[match.end() :]
    for index, char in enumerate(body):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif (
            depth == 0
            and re.match(r"\bFROM\b", body[index : index + 5], re.I)
            and (index == 0 or not (body[index - 1].isalnum() or body[index - 1] == "_"))
        ):
            return body[:index]
    return None


def _classify(expression: str) -> str:
    expr = re.sub(r"\s+", " ", expression.strip()).upper()
    while True:
        wrapped = re.match(r"^(?:ROUND|ABS|NVL|COALESCE|CAST|TO_NUMBER)\s*\((.*)\)$", expr)
        if not wrapped:
            break
        inner = _split_top_level(wrapped[1])
        if not inner:
            break
        expr = inner[0]
    if expr.startswith("SUM(") or expr.startswith("SUM ("):
        return "sum"
    if re.match(r"^COUNT\s*\(\s*DISTINCT\b", expr):
        return "distinct"
    if re.match(r"^COUNT\s*\(", expr):
        return "count"
    return "other"


def sql_aggregates(query: str) -> dict[str, str]:
    """Map each selected column alias (casefolded) to sum / count / distinct / other."""
    selected = _select_list(query or "")
    if selected is None:
        return {}
    kinds: dict[str, str] = {}
    for item in _split_top_level(selected):
        match = re.match(r"^(.*?)(?:\s+AS)?\s+\"?([A-Za-z_][\w$#]*)\"?\s*$", item, re.I | re.S)
        if match and not match[1].rstrip().endswith("."):
            expression, alias = match[1], match[2]
        else:
            expression, alias = item, item.rsplit(".", 1)[-1]
        kinds[alias.strip('"').casefold()] = _classify(expression)
    return kinds


def _aggregate_kinds(payload: dict) -> list[str]:
    columns = payload["columns"]
    source = payload.get("metabase_source")
    if isinstance(source, dict):
        from app.services.chat.metabase_evidence import _measure_keys

        _, operations = _measure_keys(str(source.get("connector_id")), source.get("query") or {})
        sources = source.get("column_sources") or []
        aggregate_indexes = [i for i, kind in enumerate(sources) if kind == "aggregation"]
        kinds = ["other"] * len(columns)
        if len(aggregate_indexes) == len(operations):
            for index, operation in zip(aggregate_indexes, operations, strict=True):
                kinds[index] = {
                    "sum": "sum",
                    "sum-where": "sum",
                    "count": "count",
                    "count-where": "count",
                    "distinct": "distinct",
                }.get(operation, "other")
        return kinds
    by_alias = sql_aggregates(str(payload.get("query") or ""))
    return [by_alias.get(str(column).casefold(), "other") for column in columns]


# ---------------------------------------------------------------------------
# Values and formats
# ---------------------------------------------------------------------------


def _number(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).strip())
    except Exception:
        return None
    return number if number.is_finite() else None


def _json_number(value: Decimal | None):
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def _readable(name: str) -> str:
    text = re.sub(r"(?<!\w)[A-Za-z_]\w* → ", "", str(name))
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text.replace("_", " "))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1].upper() + text[1:] if text else str(name)


def _infer_format(payload: dict, index: int, values: list[Decimal | None]) -> str:
    column = payload["columns"][index]
    if column in (payload.get("currency_columns") or []):
        return "currency"
    present = [value for value in values if value is not None]
    if not present:
        return "text"
    return "integer" if all(value == value.to_integral_value() for value in present) else "number"


def _source_label(tool: str, payload: dict) -> str:
    if isinstance(payload.get("metabase_source"), dict):
        return "Metabase"
    name = tool.casefold()
    if "bigquery" in name:
        return "BigQuery"
    if "celigo" in name:
        return "Celigo"
    if "suiteql" in name or "netsuite" in name or name.endswith("__ns_runcustomsuiteql"):
        return "NetSuite"
    return "Query"


def _query_block(tool: str, payload: dict) -> dict | None:
    source = payload.get("metabase_source")
    if isinstance(source, dict):
        text = describe_mbql(source.get("query") or {})
        return {"label": "Metabase query (query builder)", "text": text} if text else None
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        return None
    label = "BigQuery SQL" if "bigquery" in tool.casefold() else "SuiteQL query"
    return {"label": label, "text": query.strip()}


def _field_name(field) -> str:
    if isinstance(field, list) and field and field[0] == "field":
        target = field[-1]
        if isinstance(target, list):
            return str(target[-1])
        return str(target)
    if isinstance(field, list) and len(field) >= 3:
        return f"{field[0]}({_field_name(field[-1])})"
    return str(field)


def describe_mbql(query: dict) -> str:
    """Readable outline of an MBQL query for the card's collapsed query row."""
    stages = query.get("stages") if isinstance(query, dict) else None
    if not isinstance(stages, list) or not stages or not isinstance(stages[-1], dict):
        return ""
    stage = stages[-1]
    lines = []
    table = stage.get("source-table")
    if isinstance(table, list):
        lines.append("From " + ".".join(str(part) for part in table[-2:]))
    for join in stage.get("joins") or []:
        if isinstance(join, dict):
            source = next(
                (s.get("source-table") for s in join.get("stages") or [] if isinstance(s, dict)),
                None,
            )
            name = source[-1] if isinstance(source, list) and source else source
            lines.append(f"  join {name} as {join.get('alias')}")
    filters = stage.get("filters") or []
    if filters:
        lines.append(f"Filters: {len(filters)} condition{'s' if len(filters) != 1 else ''}")
    breakout = stage.get("breakout") or []
    if breakout:
        lines.append("Group by " + ", ".join(_field_name(field) for field in breakout))
    aggregation = stage.get("aggregation") or []
    if aggregation:
        lines.append(
            "Measure "
            + ", ".join(
                f"{a[0]}({_field_name(a[-1])})" if isinstance(a, list) and len(a) > 2 else str(a[0])
                for a in aggregation
                if isinstance(a, list) and a
            )
        )
    return "\n".join(lines)


def _column_index(payload: dict, name: str, what: str) -> int:
    columns = [str(column) for column in payload["columns"]]
    for candidate in (name, name.casefold()):
        for index, column in enumerate(columns):
            if column == candidate or column.casefold() == candidate:
                return index
    raise ValueError(f"Unknown {what} column {name!r}. Available columns: {columns}")


def _control_values(control: dict | None, columns: list[str]) -> dict[int, Decimal]:
    """Values of an ungrouped (single-row) control, matched to ``columns`` by name."""
    if control is None:
        return {}
    payload = control["payload"]
    if len(payload["rows"]) != 1:
        raise ValueError("A control result must be a single ungrouped row.")
    names = [str(column).casefold() for column in payload["columns"]]
    values = {}
    for index, column in enumerate(columns):
        if str(column).casefold() in names:
            number = _number(payload["rows"][0][names.index(str(column).casefold())])
            if number is not None:
                values[index] = number
    return values


def _totals(
    columns: list[str], numbers: list[list[Decimal | None]], kinds: list[str], control: dict[int, Decimal]
) -> tuple[list[Decimal | None], dict | None]:
    """Per-column totals and a reconciliation check against the control, if any."""
    totals: list[Decimal | None] = []
    status, notes = "ok", []
    for index, _ in enumerate(columns):
        values = [row[index] for row in numbers]
        if any(value is None for value in values) or not values:
            totals.append(control.get(index))
            continue
        summed = sum(values, Decimal(0))
        if index in control:
            totals.append(control[index])
            if summed != control[index]:
                status = "warn"
                notes.append("over" if summed > control[index] else "under")
        elif kinds[index] in {"sum", "count"}:
            totals.append(summed)
        else:
            totals.append(None)
    if not control:
        return totals, None
    if status == "ok":
        return totals, {"status": "ok", "text": "The rows add up to the overall total."}
    if "over" in notes:
        return totals, {
            "status": "warn",
            "text": "The groups overlap, so the rows add up to more than the overall total shown.",
        }
    return totals, {
        "status": "warn",
        "text": "Some of the overall total falls outside these rows, so the rows add up to less than the total.",
    }


# ---------------------------------------------------------------------------
# present_result
# ---------------------------------------------------------------------------


def _range_note(values: list[Decimal | None], label: str) -> str:
    present = [value for value in values if value is not None]
    if not present:
        return ""
    low, high = min(present), max(present)
    if low == high:
        return f"{_json_number(low)} {label.lower()} each"
    return f"{_json_number(low)}–{_json_number(high)} {label.lower()} each"


def build_present_card(spec: PresentResult, loaded: dict, control: dict | None) -> dict:
    payload = loaded["payload"]
    columns = [str(column) for column in payload["columns"]]
    rows = [list(row) for row in payload["rows"]][:_MAX_CARD_ROWS]
    kinds = _aggregate_kinds(payload)
    for name in spec.columns:
        _column_index(payload, name, "columns")
    numbers = [[_number(value) for value in row] for row in rows]
    measure_indexes = [i for i, kind in enumerate(kinds) if kind in {"sum", "count", "distinct"}]
    if not measure_indexes:
        # Detail rows (no aggregates): format numeric columns after the first, never total them.
        measure_indexes = [
            i
            for i in range(1, len(columns))
            if rows and all(row[i] is None or numbers[r][i] is not None for r, row in enumerate(rows))
        ]

    if spec.sort_by:
        sort_index = _column_index(payload, spec.sort_by, "sort_by")
        order = sorted(range(len(rows)), key=lambda r: numbers[r][sort_index] or Decimal("-Infinity"), reverse=True)
        rows, numbers = [rows[r] for r in order], [numbers[r] for r in order]

    column_meta = []
    for index, column in enumerate(columns):
        given = next((s for name, s in spec.columns.items() if name.casefold() == column.casefold()), None)
        fmt = (given.format if given and given.format else None) or (
            _infer_format(payload, index, [row[index] for row in numbers]) if index in measure_indexes else "text"
        )
        column_meta.append(
            {
                "key": column,
                "label": (given.label if given and given.label else None) or _readable(column),
                "format": fmt,
                "currency": (given.currency if given else None) or ("USD" if fmt == "currency" else None),
                "align": "right" if index in measure_indexes else "left",
            }
        )

    control_values = _control_values(control, columns)
    totals, check = (None, None)
    if spec.totals and spec.no_total_reason is None and measure_indexes:
        all_totals, check = _totals(columns, numbers, kinds, control_values)
        totals = [_json_number(all_totals[i]) if i in measure_indexes else None for i in range(len(columns))]

    out_rows = [
        [_json_number(numbers[r][i]) if i in measure_indexes else rows[r][i] for i in range(len(columns))]
        for r in range(len(rows))
    ]

    share = None
    if spec.share_of:
        share_index = _column_index(payload, spec.share_of, "share_of")
        total = totals[share_index] if totals else None
        values = [numbers[r][share_index] for r in range(len(rows))]
        if total and total > 0 and all(value is not None and value >= 0 for value in values):
            share = {
                "label": spec.share_label or f"Share of {column_meta[share_index]['label'].lower()}",
                "of": share_index,
                "values": [round(float(value / Decimal(str(total)) * 100), 1) for value in values],
            }

    plural = spec.row_label_plural or "rows"
    top_n = spec.top_n or (_TOP_N_DEFAULT if len(rows) > _TOP_N_THRESHOLD else len(rows))
    more_label = None
    if len(rows) > top_n:
        hidden = len(rows) - top_n
        first_measure = measure_indexes[0] if measure_indexes else None
        note = (
            _range_note(
                [numbers[r][first_measure] for r in range(top_n, len(rows))], column_meta[first_measure]["label"]
            )
            if first_measure is not None
            else ""
        )
        more_label = f"Show {hidden} more {plural}" + (f" · {note}" if note else "")

    tiles = []
    if spec.tiles and totals:
        for index in measure_indexes:
            if totals[index] is not None:
                tiles.append(
                    {
                        "label": column_meta[index]["label"],
                        "value": totals[index],
                        "format": column_meta[index]["format"],
                        "currency": column_meta[index]["currency"],
                    }
                )
        if spec.row_label_plural:
            tiles.append(
                {
                    "label": spec.row_label_plural[:1].upper() + spec.row_label_plural[1:],
                    "value": len(rows),
                    "format": "integer",
                }
            )

    collapsed_note = None
    if spec.collapsed or spec.no_total_reason:
        collapsed_note = f"{len(rows)} {plural}" + (", not added together" if spec.no_total_reason else "")

    return {
        "card_id": f"card-{uuid.uuid4().hex[:10]}",
        "kind": "table",
        "result_ids": [spec.result_id],
        "title": spec.title,
        "source": _source_label(loaded["tool"], payload),
        "subtitle": spec.subtitle,
        "as_of": loaded["as_of"],
        "scope": spec.scope,
        "queries": [q for q in [_query_block(loaded["tool"], payload)] if q],
        "columns": column_meta,
        "rows": out_rows,
        "row_flags": None,
        "share": share,
        "totals": totals,
        "totals_label": f"Total · {len(rows)} {plural}" if totals else None,
        "check": check,
        "tiles": tiles,
        "top_n": top_n,
        "more_label": more_label,
        "less_label": f"Show fewer {plural}",
        "collapsed": spec.collapsed,
        "collapsed_note": collapsed_note,
        "no_total_reason": spec.no_total_reason,
        "headline": None,
        "detail": None,
        "truncated": bool(payload.get("truncated")) or len(payload["rows"]) > _MAX_CARD_ROWS,
    }


# ---------------------------------------------------------------------------
# compare_results
# ---------------------------------------------------------------------------


def _clean_key(value) -> str:
    text = re.sub(r"\s*\([^)]*\)", "", str(value if value is not None else "")).strip()
    return text.split("/")[0].strip() or str(value)


def _norm_key(value) -> str:
    return re.sub(r"[^\w]+", " ", _clean_key(value).casefold()).strip()


def _join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _side(loaded: dict, key: str, measures: list[str], control: dict | None) -> dict:
    payload = loaded["payload"]
    key_index = _column_index(payload, key, "key")
    indexes = [_column_index(payload, name, "measure") for name in measures]
    kinds = _aggregate_kinds(payload)
    rows: dict[str, dict] = {}
    for row in payload["rows"]:
        norm = _norm_key(row[key_index])
        if norm in rows:
            raise ValueError(f"The key {row[key_index]!r} appears more than once; group by the key first.")
        rows[norm] = {"display": _clean_key(row[key_index]), "values": [_number(row[i]) for i in indexes]}
    numbers = [entry["values"] for entry in rows.values()]
    names = [payload["columns"][i] for i in indexes]
    totals, check = _totals(names, numbers, [kinds[i] for i in indexes], _control_values(control, names))
    return {"rows": rows, "totals": totals, "check": check, "query": _query_block(loaded["tool"], payload)}


def build_compare_card(
    spec: CompareResults, left: dict, right: dict, controls: tuple[dict | None, dict | None]
) -> tuple[dict, dict]:
    a = _side(left, spec.key.left, [m.left for m in spec.measures], controls[0])
    b = _side(right, spec.key.right, [m.right for m in spec.measures], controls[1])
    keys = list(a["rows"]) + [key for key in b["rows"] if key not in a["rows"]]
    first = 0
    keys.sort(key=lambda k: (a["rows"].get(k) or b["rows"].get(k))["values"][first] or Decimal(0), reverse=True)

    differing: dict[str, list[str]] = {m.label: [] for m in spec.measures}
    deltas: dict[str, Decimal] = {m.label: Decimal(0) for m in spec.measures}
    only_left = [a["rows"][k]["display"] for k in keys if k not in b["rows"]]
    only_right = [b["rows"][k]["display"] for k in keys if k not in a["rows"]]
    pairs = []
    for key in keys:
        left_row, right_row = a["rows"].get(key), b["rows"].get(key)
        display = (left_row or right_row)["display"]
        values = []
        for index, measure in enumerate(spec.measures):
            lv = left_row["values"][index] if left_row else None
            rv = right_row["values"][index] if right_row else None
            if lv is not None and rv is not None and lv != rv:
                differing[measure.label].append(display)
                deltas[measure.label] += rv - lv
            values.append((lv, rv))
        pairs.append((display, values, bool(left_row and right_row)))

    delta_measures = {i for i, m in enumerate(spec.measures) if differing[m.label]}
    rows, flags = [], []
    for display, values, both in pairs:
        row: list = [display]
        flag = None if both else "missing"
        for index, (lv, rv) in enumerate(values):
            row += [_json_number(lv), _json_number(rv)]
            if index in delta_measures:
                row.append(None if lv is None or rv is None else _json_number(rv - lv))
            if lv is not None and rv is not None and lv != rv:
                flag = flag or "diff"
        rows.append(row)
        flags.append(flag)

    columns = [{"key": "key", "label": spec.key_label, "format": "text", "align": "left"}]
    for index, measure in enumerate(spec.measures):
        fmt = measure.format or "integer"
        common = {
            "currency": measure.currency or ("USD" if fmt == "currency" else None),
            "align": "right",
            "group": measure.label,
        }
        columns.append({"key": f"m{index}_left", "label": spec.left_label, "format": fmt, **common})
        columns.append({"key": f"m{index}_right", "label": spec.right_label, "format": fmt, **common})
        if index in delta_measures:
            columns.append({"key": f"m{index}_delta", "label": "Difference", "format": "delta", **common})

    totals = [None]
    for index, measure in enumerate(spec.measures):
        lt, rt = a["totals"][index], b["totals"][index]
        totals += [_json_number(lt), _json_number(rt)]
        if index in delta_measures:
            totals.append(None if lt is None or rt is None else _json_number(rt - lt))

    plural = spec.key_label_plural
    headline_parts, detail_parts = [], []
    for measure in spec.measures:
        names = differing[measure.label]
        if not names:
            headline_parts.append(f"{measure.label} match {spec.left_label} in every {spec.key_label.lower()}.")
        else:
            noun = spec.key_label.lower() if len(names) == 1 else plural
            headline_parts.append(f"{measure.label} differ in {len(names)} {noun}.")
            delta = deltas[measure.label]
            direction = "more" if delta > 0 else "fewer"
            where = f"all in {_join_names(names)}" if len(names) <= 5 else f"across {len(names)} {plural}"
            if delta != 0:
                detail_parts.append(
                    f"{spec.right_label} has {_json_number(abs(delta))} {direction} {measure.label.lower()}, {where}."
                )
    if only_left:
        detail_parts.append(f"Only in {spec.left_label}: {_join_names(only_left)}.")
    if only_right:
        detail_parts.append(f"Only in {spec.right_label}: {_join_names(only_right)}.")

    visible = spec.top_n or (_TOP_N_DEFAULT if len(rows) > _TOP_N_THRESHOLD else len(rows))
    # Rows that differ are always visible; the rest fill the top slots in order.
    shown = [i for i, flag in enumerate(flags) if flag] + [i for i, flag in enumerate(flags) if not flag]
    keep = sorted(shown[: max(visible, sum(1 for f in flags if f))])
    order = keep + [i for i in range(len(rows)) if i not in keep]
    rows, flags = [rows[i] for i in order], [flags[i] for i in order]
    top_n = len(keep)
    more_label = None
    if len(rows) > top_n:
        hidden = len(rows) - top_n
        identical = all(flag is None for flag in flags[top_n:])
        more_label = f"Show {hidden} more {plural}" + (" · identical in both sources" if identical else "")

    checks = [side["check"] for side in (a, b) if side["check"]]
    check = None
    if checks:
        ok = all(c["status"] == "ok" for c in checks) and len(checks) == 2
        check = {
            "status": "ok" if ok else "warn",
            "text": f"In each source, the {spec.key_label.lower()} rows add up to that source's overall total."
            if ok
            else " ".join(c["text"] for c in checks if c["status"] != "ok") or checks[0]["text"],
        }

    card = {
        "card_id": f"card-{uuid.uuid4().hex[:10]}",
        "kind": "comparison",
        "result_ids": [spec.left_result_id, spec.right_result_id],
        "title": spec.title,
        "source": f"{spec.left_label} vs {spec.right_label}",
        "subtitle": spec.subtitle or f"Matched on {spec.key_label.lower()}",
        "as_of": right["as_of"],
        "scope": None,
        "queries": [
            {**q, "label": f"{label} query" + (" (SuiteQL)" if q["label"] == "SuiteQL query" else "")}
            for q, label in ((a["query"], spec.left_label), (b["query"], spec.right_label))
            if q
        ],
        "columns": columns,
        "rows": rows,
        "row_flags": flags,
        "share": None,
        "totals": totals,
        "totals_label": f"Total · {len(rows)} {plural}",
        "check": check,
        "tiles": [],
        "top_n": top_n,
        "more_label": more_label,
        "less_label": f"Show fewer {plural}",
        "collapsed": False,
        "collapsed_note": None,
        "no_total_reason": None,
        "headline": " ".join(headline_parts),
        "detail": " ".join(detail_parts) or None,
        "truncated": False,
    }
    facts = {
        "matching": [m.label for m in spec.measures if not differing[m.label]],
        "differing": {label: names for label, names in differing.items() if names},
        "only_in_left": only_left,
        "only_in_right": only_right,
    }
    return card, facts


# ---------------------------------------------------------------------------
# Tool entry points
# ---------------------------------------------------------------------------

_NOTE = (
    "The card is displayed to the user with its title, figures, totals and query. "
    "Do not restate its numbers, SQL, scope or column list in your answer."
)


def _error(message: str) -> dict:
    return {"error": message}


async def execute_present(params: dict, context: dict | None = None, **_kwargs) -> dict:
    try:
        spec = PresentResult.model_validate(params)
    except ValidationError as exc:
        return _error(f"Invalid present_result arguments: {exc.errors()[0]['msg']}")
    try:
        loaded = await _load(context or {}, spec.result_id)
        control = await _load(context or {}, spec.control_result_id) if spec.control_result_id else None
        card = build_present_card(spec, loaded, control)
    except ValueError as exc:
        return _error(str(exc))
    return json.loads(
        json.dumps(
            {"result_card": card, "llm": {"card_shown": True, "card_id": card["card_id"], "note": _NOTE}}, default=str
        )
    )


async def execute_compare(params: dict, context: dict | None = None, **_kwargs) -> dict:
    try:
        spec = CompareResults.model_validate(params)
    except ValidationError as exc:
        return _error(f"Invalid compare_results arguments: {exc.errors()[0]['msg']}")
    try:
        left = await _load(context or {}, spec.left_result_id)
        right = await _load(context or {}, spec.right_result_id)
        controls = (
            await _load(context or {}, spec.left_control_result_id) if spec.left_control_result_id else None,
            await _load(context or {}, spec.right_control_result_id) if spec.right_control_result_id else None,
        )
        card, facts = build_compare_card(spec, left, right, controls)
    except ValueError as exc:
        return _error(str(exc))
    return json.loads(
        json.dumps(
            {
                "result_card": card,
                "llm": {
                    "card_shown": True,
                    "card_id": card["card_id"],
                    "note": _NOTE + " The headline above the card already states which measures match and differ.",
                    **facts,
                },
            },
            default=str,
        )
    )
