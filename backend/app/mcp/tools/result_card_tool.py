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

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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

    @model_validator(mode="after")
    def _unique_labels(self):
        labels = [m.label.casefold() for m in self.measures]
        if len(set(labels)) != len(labels):
            raise ValueError("Each measure needs its own label.")
        return self


# ---------------------------------------------------------------------------
# Loading stored results
# ---------------------------------------------------------------------------


class _Loader:
    """Resolves result ids for one tool call: authorises once, reads history once.

    Same-turn results come from the full-payload sidecar; earlier turns from the
    persisted assistant messages, numbered exactly like
    ``resolve_payload_from_messages`` (explicit result_id first, then position).
    """

    def __init__(self, context: dict):
        self.context = context
        self.auth = None
        self.messages = None

    async def load(self, rid: str) -> dict[str, Any]:
        from app.mcp.tools.result_pivot import _authorize
        from app.services.chat.result_cache import get_full_payload_entry
        from app.services.chat.tool_call_results import load_conversation_tool_messages

        if self.auth is None:
            self.auth = await _authorize(self.context)
        db, tenant, session = self.auth
        entry = None
        try:
            sidecar = await asyncio.to_thread(get_full_payload_entry, str(session), rid)
        except Exception:
            sidecar = None
        if isinstance(sidecar, dict) and isinstance(sidecar.get("payload"), dict):
            entry = {"payload": sidecar["payload"], "tool": sidecar.get("tool") or "", "as_of": None}
        if entry is None:
            if self.messages is None:
                self.messages = await load_conversation_tool_messages(db, session, tenant)
            positional, fallback = 0, None
            for message in self.messages:
                for call in message.tool_calls if isinstance(message.tool_calls, list) else []:
                    if not isinstance(call, dict) or not isinstance(call.get("result_payload"), dict):
                        continue
                    positional += 1
                    found = {
                        "payload": call["result_payload"],
                        "tool": call.get("tool") or "",
                        "as_of": message.created_at,
                    }
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
    if payload.get("source_kind") == "metabase" and not isinstance(source, dict):
        # A pivot of Metabase results carries no bound connector to re-check, and it
        # is already shown as its own table.
        raise ValueError("A pivoted result is already displayed; present the result it was pivoted from instead.")
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
    if policy.tool_allowlist and (not tool or tool not in policy.tool_allowlist):
        raise ValueError("The current policy no longer permits this result's source.")
    blocked = {str(name).casefold() for name in (policy.blocked_fields or [])}
    names = [*payload["columns"], *((source or {}).get("column_names") or [])]
    query_words = set(re.findall(r"[A-Za-z_][\w$#]*", str(payload.get("query") or "").casefold()))
    if any(str(name).casefold() in blocked for name in names) or blocked & query_words:
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


def _call_args(expr: str, names: str) -> str | None:
    """The argument text when ``expr`` is exactly one call to ``names`` (nothing outside it)."""
    match = re.match(rf"^(?:{names})\s*\(", expr)
    if not match:
        return None
    depth, quote = 0, None
    for index in range(match.end() - 1, len(expr)):
        char = expr[index]
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return expr[match.end() : index] if index == len(expr) - 1 else None
    return None


def _classify(expression: str) -> str:
    """Fail closed: only a lone SUM(...) / COUNT(...) is additive across groups.

    Arithmetic over aggregates (ratios, margins), SUM(DISTINCT ...) and anything
    unrecognised is "other" and is totalled only from an ungrouped control result.
    Wrappers that keep a sum additive are peeled: ROUND(x, n), NVL/COALESCE(x, 0),
    TO_NUMBER(x). ABS is not: the absolute values of group sums do not add up.
    """
    expr = re.sub(r"\s+", " ", expression.strip()).upper()
    while True:
        args = _call_args(expr, "ROUND|NVL|COALESCE|TO_NUMBER")
        if args is None:
            break
        parts = _split_top_level(args)
        if not parts or any(not re.fullmatch(r"-?\d+", part) for part in parts[1:]):
            return "other"
        if expr.startswith(("NVL", "COALESCE")) and any(part != "0" for part in parts[1:]):
            return "other"
        expr = parts[0]
    args = _call_args(expr, "SUM")
    if args is not None:
        return "other" if re.match(r"^\s*DISTINCT\b", args) else "sum"
    args = _call_args(expr, "COUNT")
    if args is not None:
        return "distinct" if re.match(r"^\s*DISTINCT\b", args) else "count"
    return "other"


def _strip_sql_comments(query: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", re.sub(r"--[^\n]*", " ", query), flags=re.S)


def _top_level_words(query: str) -> set[str]:
    words, depth, quote, token = set(), 0, None, []
    for char in query + " ":
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if depth == 0 and (char.isalnum() or char == "_"):
            token.append(char)
        elif token:
            words.add("".join(token).upper())
            token = []
    return words


_SQL_LIMIT = re.compile(
    r"\bFETCH\s+(?:FIRST|NEXT)\s+(\d+)\s+ROWS?\s+ONLY\b|\bLIMIT\s+(\d+)\b|\bTOP\s+(\d+)\b|\bROWNUM\s*<\s*(=?)\s*(\d+)",
    re.I,
)


def _depth_at(text: str, position: int) -> int:
    depth, quote = 0, None
    for char in text[:position]:
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
    return depth


def _mbql_limits(node) -> list[int]:
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "limit" and isinstance(value, int) and not isinstance(value, bool):
                found.append(value)
            else:
                found += _mbql_limits(value)
    elif isinstance(node, list):
        for item in node:
            found += _mbql_limits(item)
    return found


def _final_stage(query):
    """The stage that produced the rows: the last pMBQL stage, or legacy MBQL's inner query."""
    if not isinstance(query, dict):
        return None
    stages = query.get("stages")
    if isinstance(stages, list) and stages and isinstance(stages[-1], dict):
        return stages[-1]
    if isinstance(query.get("query"), dict):
        return query["query"]
    return query


def _is_partial(payload: dict) -> bool:
    """A result is partial when the source says so, or when its own query capped it.

    NetSuite reports a ``FETCH FIRST n`` result as complete, so n rows back means more
    may exist. A limit inside a subquery caps the input to everything outside it, so it
    always makes the result partial.
    """
    if payload.get("truncated"):
        return True
    returned = len(payload.get("rows") or [])
    source = payload.get("metabase_source")
    if isinstance(source, dict):
        # A limit anywhere but the final stage caps that stage's input: always partial.
        limits = _mbql_limits(source.get("query"))
        final = _final_stage(source.get("query"))
        own = final.get("limit") if isinstance(final, dict) else None
        if isinstance(own, int) and not isinstance(own, bool):
            limits.remove(own)
            if returned >= own:
                return True
        return bool(limits)
    text = _strip_sql_comments(str(payload.get("query") or ""))
    for match in _SQL_LIMIT.finditer(text):
        if _depth_at(text, match.start()) > 0:
            return True
        if match[4] is not None:
            limit = int(match[5]) - (0 if match[4] else 1)
        else:
            limit = int(next(group for group in match.groups()[:3] if group))
        if returned >= limit:
            return True
    return False


def sql_aggregates(query: str) -> dict[str, str]:
    """Map each selected column alias (casefolded) to sum / count / distinct / other.

    Only a plain single SELECT is read. CTEs and set operations (UNION, INTERSECT,
    MINUS, EXCEPT) return {} -- every column "other" -- because the first SELECT in
    the text is then not the one that produced the result.
    """
    text = _strip_sql_comments(query or "").strip()
    if re.match(r"^WITH\b", text, re.I) or _top_level_words(text) & {"UNION", "INTERSECT", "MINUS", "EXCEPT"}:
        return {}
    selected = _select_list(text)
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
    # A money-looking column without a known currency code is a plain number: the
    # card never guesses USD (a mixed-currency column would otherwise read as dollars).
    present = [value for value in values if value is not None]
    if not present:
        return "text"
    return "integer" if all(value == value.to_integral_value() for value in present) else "number"


def _source_label(tool: str, payload: dict) -> str:
    kind = str(payload.get("source_kind") or "").casefold()
    if isinstance(payload.get("metabase_source"), dict) or kind == "metabase":
        return "Metabase"
    if kind == "bigquery":
        return "BigQuery"
    if kind == "suiteql":
        return "NetSuite"
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
    label = "BigQuery SQL" if _source_label(tool, payload) == "BigQuery" else "SuiteQL query"
    if _source_label(tool, payload) not in {"BigQuery", "NetSuite"}:
        label = "Query"
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


def _has_breakout(node) -> bool:
    if isinstance(node, dict):
        return any((key == "breakout" and value) or _has_breakout(value) for key, value in node.items())
    if isinstance(node, list):
        return any(_has_breakout(item) for item in node)
    return False


def _is_overall(control: dict, tool: str) -> bool:
    """True only when the control provably is one overall figure for the same source:
    one row, not partial, and an ungrouped query whose every column is an aggregate."""
    payload = control["payload"]
    if control.get("tool") != tool or len(payload["rows"]) != 1 or _is_partial(payload):
        return False
    source = payload.get("metabase_source")
    if isinstance(source, dict):
        sources = source.get("column_sources") or []
        return (
            isinstance(source.get("query"), dict)
            and not _has_breakout(source["query"])
            and len(sources) == len(payload["columns"])
            and all(kind == "aggregation" for kind in sources)
        )
    text = _strip_sql_comments(str(payload.get("query") or "")).strip()
    if not text or re.match(r"^WITH\b", text, re.I):
        return False
    if _top_level_words(text) & {"GROUP", "UNION", "INTERSECT", "MINUS", "EXCEPT"}:
        return False
    selected = _select_list(text)
    if selected is None:
        return False
    return all(re.search(r"\b(?:SUM|COUNT|AVG|MIN|MAX)\s*\(", item, re.I) for item in _split_top_level(selected))


def _canon(text: str) -> str:
    """SQL text compared for meaning, not layout: case and spacing folded outside quotes."""
    parts = re.split(r"('(?:[^']|'')*')", text)
    out = []
    for index, part in enumerate(parts):
        if index % 2:
            out.append(part)
            continue
        part = re.sub(r"\s+", " ", part.casefold())
        out.append(re.sub(r"\s*([(),=<>+*/-])\s*", r"\1", part))
    return "".join(out).strip()


def _canon_measure(expression: str) -> str:
    """An aggregate's expression without wrappers that do not change what it counts:
    ROUND(x, n), NVL/COALESCE(x, 0), TO_NUMBER(x)."""
    expr = _canon(expression)
    while True:
        args = _call_args(expr, "round|nvl|coalesce|to_number")
        if args is None:
            return expr
        parts = _split_top_level(args)
        if not parts or any(not re.fullmatch(r"-?\d+", part) for part in parts[1:]):
            return expr
        expr = parts[0]


def _top_level_tokens(text: str) -> list[tuple[int, str]]:
    tokens, depth, quote, start = [], 0, None, None
    for index, char in enumerate(text + " "):
        if quote:
            if char == quote:
                quote = None
            continue
        word = depth == 0 and (char.isalnum() or char == "_")
        if word and start is None:
            start = index
        elif not word and start is not None:
            tokens.append((start, text[start:index].upper()))
            start = None
        if char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
    return tokens


def _sql_shape(query: str) -> tuple[str, dict[str, str]] | None:
    """(population, alias -> aggregate) of a plain SELECT. The population is the FROM clause
    up to GROUP BY / HAVING / ORDER BY / a row limit: the tables, joins and filters that
    decide which rows are counted."""
    text = _strip_sql_comments(query or "").strip()
    if (
        not text
        or re.match(r"^WITH\b", text, re.I)
        or _top_level_words(text) & {"UNION", "INTERSECT", "MINUS", "EXCEPT"}
    ):
        return None
    tokens = _top_level_tokens(text)
    starts = [start for start, word in tokens if word == "FROM"]
    if not starts:
        return None
    ends = [s for s, w in tokens if s > starts[0] and w in {"GROUP", "HAVING", "ORDER", "FETCH", "LIMIT", "OFFSET"}]
    population = _canon(text[starts[0] : ends[0] if ends else len(text)])
    selected = _select_list(text)
    if selected is None:
        return None
    measures = {}
    for item in _split_top_level(selected):
        match = re.match(r"^(.*?)(?:\s+AS)?\s+\"?([A-Za-z_][\w$#]*)\"?\s*$", item, re.I | re.S)
        if match and not match[1].rstrip().endswith("."):
            measures[match[2].casefold()] = _canon_measure(match[1])
        else:
            measures[item.rsplit(".", 1)[-1].strip('"').casefold()] = _canon_measure(item)
    return population, measures


def _mbql_population(query) -> str | None:
    """The query with only the final stage's grouping, ordering and limit removed."""
    import copy
    import json

    if not isinstance(query, dict):
        return None
    query = copy.deepcopy(query)
    final = _final_stage(query)
    if isinstance(final, dict):
        for key in ("breakout", "order-by", "limit", "fields", "page"):
            final.pop(key, None)
    return json.dumps(query, sort_keys=True, default=str)


def _counts_the_same_rows(control: dict, shown: dict, names: list[str]) -> bool:
    """True when the control is the shown query without its grouping: same tables, joins
    and filters, and the same aggregate for every column it totals."""
    control_source, shown_source = control.get("metabase_source"), shown.get("metabase_source")
    if isinstance(control_source, dict) or isinstance(shown_source, dict):
        return (
            isinstance(control_source, dict)
            and isinstance(shown_source, dict)
            and control_source.get("connector_id") == shown_source.get("connector_id")
            and _mbql_population(control_source.get("query")) is not None
            and _mbql_population(control_source.get("query")) == _mbql_population(shown_source.get("query"))
        )
    control_shape = _sql_shape(str(control.get("query") or ""))
    shown_shape = _sql_shape(str(shown.get("query") or ""))
    if control_shape is None or shown_shape is None or control_shape[0] != shown_shape[0]:
        return False
    shared = [name.casefold() for name in names if name.casefold() in control_shape[1]]
    return all(shown_shape[1].get(name) == control_shape[1][name] for name in shared)


def _control_values(control: dict | None, loaded: dict, columns: list[str]) -> dict[int, Decimal]:
    """Values of an overall control, matched to ``columns`` by name."""
    if control is None:
        return {}
    payload = control["payload"]
    if not _is_overall(control, loaded["tool"]):
        raise ValueError(
            "A control result must be an overall figure from the same source: one row from an "
            "ungrouped, uncapped query whose columns are all aggregates (no GROUP BY or breakout)."
        )
    if not _counts_the_same_rows(payload, loaded["payload"], columns):
        raise ValueError(
            "A control result must count the same rows as the result it totals: run the same "
            "query without its grouping (same FROM, joins and WHERE, and the same aggregate for "
            "each column)."
        )
    names = [str(column).casefold() for column in payload["columns"]]
    values = {}
    for index, column in enumerate(columns):
        if str(column).casefold() in names:
            number = _number(payload["rows"][0][names.index(str(column).casefold())])
            if number is not None:
                values[index] = number
    return values


def _totals(
    columns: list[str],
    numbers: list[list[Decimal | None]],
    control: dict[int, Decimal],
    kinds: list[str] | None = None,
) -> tuple[list[Decimal | None], dict | None]:
    """Totals come only from the source's own ungrouped control result, never from
    adding the rows up here: the rows may be a capped page (FETCH FIRST n), mixed
    currencies or a ratio, and only the source knows the real overall figure.

    The check then states how the rows relate to that figure -- add up, more (groups
    overlap), less (part of the total is outside these rows), or cannot be checked
    (a row has no value). Only sums, counts and distinct counts are checked: the rows
    of an average or a ratio are not meant to add up to its overall value. Rounded
    rows may differ from the total by up to half a unit of their precision each.
    """
    totals: list[Decimal | None] = [control.get(index) for index in range(len(columns))]
    checkable = {
        index
        for index in control
        if kinds is None or (index < len(kinds) and kinds[index] in {"sum", "count", "distinct"})
    }
    if not checkable:
        return totals, None
    outcomes = set()
    totals = list(totals)
    for index in checkable:
        total = control[index]
        values = [row[index] for row in numbers]
        if not values or any(value is None for value in values):
            outcomes.add("blank")
            continue
        summed = sum(values, Decimal(0))
        places = max([-v.as_tuple().exponent for v in values if v.as_tuple().exponent < 0] or [0])
        tolerance = Decimal(len(values)) * Decimal(5).scaleb(-(places + 1)) if places else Decimal(0)
        delta = summed - total
        if delta > tolerance and kinds is not None and kinds[index] in {"sum", "count"}:
            # Groups of a sum or a count cannot overlap: this figure is not their total.
            totals[index] = None
            outcomes.add("uncovered")
            continue
        outcomes.add("ok" if abs(delta) <= tolerance else "over" if delta > 0 else "under")
    messages = {
        "blank": "Some rows have no value, so they cannot be checked against the overall total.",
        "uncovered": (
            "The rows add up to more than the overall figure given, so it does not cover these rows; "
            "no total is shown for them."
        ),
        "over": "The rows add up to more than the overall total, so the groups overlap.",
        "under": "The rows add up to less than the overall total; part of it falls outside these rows.",
    }
    problems = [messages[key] for key in ("blank", "uncovered", "over", "under") if key in outcomes]
    if not problems:
        return totals, {"status": "ok", "text": "The rows add up to the overall total."}
    return totals, {"status": "warn", "text": " ".join(problems)}


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
    rows = [list(row) for row in payload["rows"]]
    kinds = _aggregate_kinds(payload)
    for name in spec.columns:
        _column_index(payload, name, "columns")
    numbers = [[_number(value) for value in row] for row in rows]
    control_values = _control_values(control, loaded, columns)
    partial = _is_partial(payload)
    row_count = payload.get("row_count")
    row_total = row_count if partial and isinstance(row_count, int) and row_count >= len(rows) else len(rows)

    def given(column: str) -> ColumnSpec | None:
        return next((c for name, c in spec.columns.items() if name.casefold() == column.casefold()), None)

    # Measures are only formatted, never added up. A column is a measure when it is a
    # server aggregate, has an overall total, or the model gave it a numeric format;
    # anything else keeps its raw text (identifiers such as "00123" stay verbatim).
    numeric_formats = {"integer", "number", "currency", "percent"}
    measure_indexes = [
        i
        for i, column in enumerate(columns)
        if kinds[i] in {"sum", "count", "distinct"}
        or i in control_values
        or ((g := given(column)) is not None and g.format in numeric_formats)
    ]

    if spec.sort_by:
        sort_index = _column_index(payload, spec.sort_by, "sort_by")

        def sort_key(r: int) -> Decimal:
            value = numbers[r][sort_index]
            return Decimal("-Infinity") if value is None else value

        order = sorted(range(len(rows)), key=sort_key, reverse=True)
        rows, numbers = [rows[r] for r in order], [numbers[r] for r in order]

    column_meta = []
    for index, column in enumerate(columns):
        g = given(column)
        fmt = (g.format if g and g.format else None) or (
            _infer_format(payload, index, [row[index] for row in numbers]) if index in measure_indexes else "text"
        )
        currency = g.currency if g else None
        if fmt == "currency" and not currency:
            fmt = "number"  # never guess a currency
        column_meta.append(
            {
                "key": column,
                "label": (g.label if g and g.label else None) or _readable(column),
                "format": fmt,
                "currency": currency,
                "align": "right" if index in measure_indexes else "left",
            }
        )

    totals, check = (None, None)
    if spec.totals and spec.no_total_reason is None and control_values:
        all_totals, check = _totals(columns, numbers, control_values, kinds)
        totals = [_json_number(all_totals[i]) for i in range(len(columns))]
        if all(value is None for value in totals):
            totals = None

    shown = min(len(rows), _MAX_CARD_ROWS)

    def cell(r: int, i: int):
        # A measure cell that is not a number keeps its source text, never a blank.
        if i in measure_indexes and numbers[r][i] is not None:
            return _json_number(numbers[r][i])
        return rows[r][i]

    out_rows = [[cell(r, i) for i in range(len(columns))] for r in range(shown)]
    unreadable = any(
        rows[r][i] is not None and str(rows[r][i]).strip() != "" and numbers[r][i] is None
        for r in range(len(rows))
        for i in measure_indexes
    )
    if unreadable:
        note = "Some values are not plain numbers and are shown as the source returned them."
        check = (
            {"status": "warn", "text": f"{check['text']} {note}" if check["status"] == "warn" else note}
            if check
            else {"status": "warn", "text": note}
        )

    share = None
    share_index = _column_index(payload, spec.share_of, "share_of") if spec.share_of else None
    # A share of the total is meaningful only for a measure whose rows add up to it.
    if share_index is not None and totals and kinds[share_index] in {"sum", "count"}:
        total = _number(totals[share_index])
        values = [numbers[r][share_index] for r in range(shown)]
        if total and total > 0 and all(value is not None and value >= 0 for value in values):
            share = {
                "label": spec.share_label or f"Share of {column_meta[share_index]['label'].lower()}",
                "of": share_index,
                "values": [round(float(value / total * 100), 1) for value in values],
            }

    plural = spec.row_label_plural or "rows"
    top_n = min(spec.top_n or (_TOP_N_DEFAULT if shown > _TOP_N_THRESHOLD else shown), shown)
    more_label = None
    if shown > top_n:
        hidden = shown - top_n
        first_measure = measure_indexes[0] if measure_indexes else None
        note = (
            _range_note([numbers[r][first_measure] for r in range(top_n, shown)], column_meta[first_measure]["label"])
            if first_measure is not None
            else ""
        )
        more_label = f"Show {hidden} more {plural}" + (f" · {note}" if note else "")

    tiles = []
    if spec.tiles:
        for index in measure_indexes:
            if totals and totals[index] is not None:
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
                    "value": row_total,
                    "format": "integer",
                }
            )

    collapsed_note = None
    if spec.collapsed or spec.no_total_reason:
        collapsed_note = f"{row_total} {plural}" + (", not added together" if spec.no_total_reason else "")

    return {
        "card_id": f"card-{uuid.uuid4().hex[:10]}",
        "kind": "table",
        "result_ids": [spec.result_id],
        "control_result_ids": [spec.control_result_id] if spec.control_result_id else [],
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
        "totals_label": f"Total · {row_total} {plural}" if totals else None,
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
        "truncated": partial or len(rows) > _MAX_CARD_ROWS,
    }


# ---------------------------------------------------------------------------
# compare_results
# ---------------------------------------------------------------------------


def _clean_key(value) -> str:
    """A key without a worded qualifier: "New Zealand/Aotearoa" -> "New Zealand", "Taiwan
    (Province of China)" -> "Taiwan". A suffix with a digit is part of an identifier ("A/1",
    "Line (2)") and is kept."""
    text = str(value if value is not None else "")
    text = re.sub(r"\s*\((?=[^)]*[A-Za-z])[^)\d]*\)", "", text).strip()
    head, slash, tail = text.partition("/")
    if slash and tail.strip() and not re.search(r"\d", tail) and re.search(r"[A-Za-z]", head):
        text = head
    return text.strip() or str(value)


def _full_key(value) -> str:
    """Exact key: case and spacing are folded; punctuation is kept ("A-1" is not "A/1")."""
    return re.sub(r"\s+", " ", str(value if value is not None else "").casefold()).strip()


def _norm_key(value) -> str:
    return _full_key(_clean_key(value))


def _join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _side(loaded: dict, key: str, measures: list[str], control: dict | None) -> dict:
    payload = loaded["payload"]
    key_index = _column_index(payload, key, "key")
    indexes = [_column_index(payload, name, "measure") for name in measures]
    rows: dict[str, dict] = {}
    for row in payload["rows"]:
        raw = row[key_index]
        full = _full_key(raw)
        if full in rows:
            shown = "a blank value" if not full else repr(raw)
            raise ValueError(f"The key has {shown} more than once; group by the key first.")
        cells = [row[i] for i in indexes]
        rows[full] = {
            "raw": "(blank)" if raw is None or not str(raw).strip() else str(raw),
            "values": [_number(cell) for cell in cells],
            "cells": cells,
            # A cell with content that is not a number cannot be compared.
            "unreadable": [cell is not None and str(cell).strip() != "" and _number(cell) is None for cell in cells],
        }
    numbers = [entry["values"] for entry in rows.values()]
    names = [payload["columns"][i] for i in indexes]
    kinds = _aggregate_kinds(payload)
    totals, check = _totals(names, numbers, _control_values(control, loaded, names), [kinds[i] for i in indexes])
    return {
        "rows": rows,
        "kinds": [kinds[i] for i in indexes],
        "totals": totals,
        "check": check,
        "partial": _is_partial(payload),
        "query": _query_block(loaded["tool"], payload),
    }


def _pair_keys(a_rows: dict, b_rows: dict) -> list[tuple[str | None, str | None, str]]:
    """Pair keys across sources. Exact (normalised) keys pair first. A key then pairs with
    its bare form only: "New Zealand" with "New Zealand/Aotearoa", "Taiwan" with "Taiwan
    (Province of China)". Two qualified names ("Congo (Kinshasa)", "Congo (Brazzaville)")
    never pair, whatever is on the other side. Each key is normalised once."""
    left_bare = {k: _norm_key(entry["raw"]) for k, entry in a_rows.items()}
    right_by_bare: dict[str, list[str]] = {}
    for k, entry in b_rows.items():
        right_by_bare.setdefault(_norm_key(entry["raw"]), []).append(k)
    left_count: dict[str, int] = {}
    for bare in left_bare.values():
        left_count[bare] = left_count.get(bare, 0) + 1
    pairs: dict[str, str] = {k: k for k in a_rows if k in b_rows}
    used_right = set(pairs.values())
    fallback: set[str] = set()
    for k in a_rows:
        if k in pairs:
            continue
        bare = left_bare[k]
        candidates = [r for r in right_by_bare.get(bare, []) if r not in used_right and (k == bare or r == bare)]
        if len(candidates) == 1 and left_count[bare] == 1:
            pairs[k] = candidates[0]
            used_right.add(candidates[0])
            fallback.add(k)
    out: list[tuple[str | None, str | None, str]] = []
    for k in a_rows:
        raw = a_rows[k]["raw"]
        out.append((k, pairs.get(k), _clean_key(raw) if k in fallback else raw))
    for k in b_rows:
        if k not in used_right:
            out.append((None, k, b_rows[k]["raw"]))
    return out


_QUERY_KIND = {"SuiteQL query": "SuiteQL", "BigQuery SQL": "SQL", "Metabase query (query builder)": "query builder"}


def _detail_for(
    measure: str,
    right_label: str,
    deltas: list[tuple[Decimal, str]],
    plural: str,
    overall: Decimal | None,
    additive: bool = True,
) -> str | None:
    """Name where a measure differs, largest first. A figure is stated only from the two
    sources' own overall totals, and only for a sum or a count: a distinct count or a ratio
    does not add up across keys (an order can sit under several SKUs), so its overall
    difference is neither sized nor placed "all in" the keys whose values changed."""
    more = sorted(((d, n) for d, n in deltas if d > 0), key=lambda item: -item[0])
    fewer = sorted(((-d, n) for d, n in deltas if d < 0), key=lambda item: -item[0])

    def where(items: list[tuple[Decimal, str]]) -> str:
        names = [n for _, n in items]
        return _join_names(names) if len(names) <= 5 else f"{len(names)} {plural}"

    label = measure.lower()
    if more and fewer:
        return f"{right_label} has more {label} in {where(more)} and fewer in {where(fewer)}."
    if not (more or fewer):
        return None
    direction, items = ("more", more) if more else ("fewer", fewer)
    if not additive:
        return f"{right_label} has {direction} {label} in {where(items)}."
    scope = f"all in {where(items)}" if len(items) <= 5 else f"across {where(items)}"
    if overall is not None and overall != 0 and (overall > 0) == bool(more):
        return f"{right_label} has {_json_number(abs(overall))} {direction} {label}, {scope}."
    return f"{right_label} has {direction} {label}, {scope}."


def build_compare_card(
    spec: CompareResults, left: dict, right: dict, controls: tuple[dict | None, dict | None]
) -> tuple[dict, dict]:
    a = _side(left, spec.key.left, [m.left for m in spec.measures], controls[0])
    b = _side(right, spec.key.right, [m.right for m in spec.measures], controls[1])
    paired = _pair_keys(a["rows"], b["rows"])

    def entry_of(lk, rk):
        return (
            a["rows"].get(lk) if lk is not None else None,
            b["rows"].get(rk) if rk is not None else None,
        )

    def first_value(item):
        left_row, right_row = entry_of(item[0], item[1])
        value = (left_row or right_row)["values"][0]
        return Decimal("-Infinity") if value is None else value

    paired.sort(key=first_value, reverse=True)

    # A value present on one side only, a key in one source only, or a value that is
    # not a number is a difference -- never a silent match.
    differing: dict[str, list[tuple[Decimal, str]]] = {m.label: [] for m in spec.measures}
    signed: dict[str, list[tuple[Decimal, str]]] = {m.label: [] for m in spec.measures}
    blank: dict[str, list[str]] = {m.label: [] for m in spec.measures}
    partial = a["partial"] or b["partial"]
    # Keys absent from a partial result were never fetched: not listed as differences.
    only_left = [] if b["partial"] else [display for lk, rk, display in paired if rk is None]
    only_right = [] if a["partial"] else [display for lk, rk, display in paired if lk is None]
    entries = []
    for lk, rk, display in paired:
        left_row, right_row = entry_of(lk, rk)
        both = left_row is not None and right_row is not None
        values, row_differs = [], False
        for index, measure in enumerate(spec.measures):
            lv = left_row["values"][index] if left_row else None
            rv = right_row["values"][index] if right_row else None
            unreadable = any(side["unreadable"][index] for side in (left_row, right_row) if side)
            if not both:
                # Absent from a complete result is definite; absent from a partial one was
                # never fetched.
                if not (b["partial"] if left_row is not None else a["partial"]):
                    differing[measure.label].append((Decimal(0), display))
            elif unreadable or lv is None or rv is None:
                differing[measure.label].append((Decimal(0), display))
                blank[measure.label].append(display)
                row_differs = True
            elif lv is not None and lv != rv:
                differing[measure.label].append((abs(rv - lv), display))
                signed[measure.label].append((rv - lv, display))
                row_differs = True
            lcell = left_row["cells"][index] if left_row else None
            rcell = right_row["cells"][index] if right_row else None
            values.append((lv, rv, lcell, rcell))
        flag = "missing" if not both else "diff" if row_differs else None
        entries.append((display, values, flag))

    delta_measures = {i for i, m in enumerate(spec.measures) if signed[m.label]}
    rows, flags = [], []
    for display, values, flag in entries:
        row: list = [display]
        for index, (lv, rv, lcell, rcell) in enumerate(values):
            # A value that is not a number keeps its source text.
            row += [_json_number(lv) if lv is not None else lcell, _json_number(rv) if rv is not None else rcell]
            if index in delta_measures:
                row.append(None if lv is None or rv is None else _json_number(rv - lv))
        rows.append(row)
        flags.append(flag)

    columns = [{"key": "key", "label": spec.key_label, "format": "text", "align": "left"}]
    for index, measure in enumerate(spec.measures):
        fmt = measure.format or "integer"
        if fmt == "currency" and not measure.currency:
            fmt = "number"
        common = {"currency": measure.currency, "align": "right", "group": measure.label}
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
    key_noun = spec.key_label.lower()
    headline_parts, detail_parts = [], []
    for measure in spec.measures:
        names = differing[measure.label]
        if not names:
            scope = f"every {key_noun} in both results" if partial else f"every {key_noun}"
            headline_parts.append(f"{measure.label} match {spec.left_label} in {scope}.")
            continue
        noun = key_noun if len(names) == 1 else plural
        headline_parts.append(f"{measure.label} differ in {len(names)} {noun}.")
        index = spec.measures.index(measure)
        lt, rt = a["totals"][index], b["totals"][index]
        overall = rt - lt if lt is not None and rt is not None and not (only_left or only_right or partial) else None
        additive = all(side["kinds"][index] in {"sum", "count"} for side in (a, b))
        detail = _detail_for(measure.label, spec.right_label, signed[measure.label], plural, overall, additive)
        if detail:
            detail_parts.append(detail)
        if blank[measure.label]:
            detail_parts.append(
                f"{measure.label} cannot be compared for {_join_names(blank[measure.label])}: "
                "a value is blank or not a number."
            )
    if only_left:
        detail_parts.append(f"Only in {spec.left_label}: {_join_names(only_left)}.")
    if only_right:
        detail_parts.append(f"Only in {spec.right_label}: {_join_names(only_right)}.")
    if partial:
        detail_parts.append(f"One of the results is partial, so {plural} beyond it were not compared.")

    visible = spec.top_n or (_TOP_N_DEFAULT if len(rows) > _TOP_N_THRESHOLD else len(rows))
    # Rows that differ are always visible (up to the display cap); the rest fill in order.
    flagged = [i for i, flag in enumerate(flags) if flag]
    rest = [i for i, flag in enumerate(flags) if not flag]
    keep_set = set((flagged + rest)[: min(_MAX_CARD_ROWS, max(visible, len(flagged)))])
    order = [i for i in range(len(rows)) if i in keep_set] + [i for i in range(len(rows)) if i not in keep_set]
    order = order[:_MAX_CARD_ROWS]
    top_n = min(len(keep_set), len(order))
    capped = len(rows) > _MAX_CARD_ROWS
    rows, flags = [rows[i] for i in order], [flags[i] for i in order]
    more_label = None
    if len(rows) > top_n:
        hidden = len(rows) - top_n
        identical = all(flag is None for flag in flags[top_n:])
        more_label = f"Show {hidden} more {plural}" + (" · identical in both sources" if identical else "")

    # The check names each source; one checked side never reads as both.
    side_checks = [(spec.left_label, a["check"]), (spec.right_label, b["check"])]
    check = None
    if any(c for _, c in side_checks):
        if all(c and c["status"] == "ok" for _, c in side_checks):
            check = {
                "status": "ok",
                "text": f"In each source, the {key_noun} rows add up to that source's overall total.",
            }
        else:
            parts = []
            for label, c in side_checks:
                if c is None:
                    parts.append(f"{label}: no overall total to check the rows against.")
                elif c["status"] == "ok":
                    parts.append(f"{label}: the {key_noun} rows add up to the overall total.")
                else:
                    parts.append(f"{label}: {c['text']}")
            check = {"status": "warn", "text": " ".join(parts)}

    card = {
        "card_id": f"card-{uuid.uuid4().hex[:10]}",
        "kind": "comparison",
        "result_ids": [spec.left_result_id, spec.right_result_id],
        "control_result_ids": [rid for rid in (spec.left_control_result_id, spec.right_control_result_id) if rid],
        "title": spec.title,
        "source": f"{spec.left_label} vs {spec.right_label}",
        "subtitle": spec.subtitle or f"Matched on {key_noun}",
        "as_of": right["as_of"],
        "scope": None,
        "queries": [
            {**q, "label": f"{label} query ({_QUERY_KIND.get(q['label'], 'query')})"}
            for q, label in ((a["query"], spec.left_label), (b["query"], spec.right_label))
            if q
        ],
        "columns": columns,
        "rows": rows,
        "row_flags": flags,
        "share": None,
        "totals": totals if any(value is not None for value in totals) else None,
        "totals_label": f"Total · {len(paired)} {plural}" if any(value is not None for value in totals) else None,
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
        "truncated": partial or capped,
    }
    facts = {
        "matching": [m.label for m in spec.measures if not differing[m.label]],
        "differing": {label: [n for _, n in items] for label, items in differing.items() if items},
        "only_in_left": only_left,
        "only_in_right": only_right,
        "partial": partial,
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
    loader = _Loader(context or {})
    try:
        loaded = await loader.load(spec.result_id)
        control = await loader.load(spec.control_result_id) if spec.control_result_id else None
        card = build_present_card(spec, loaded, control)
    except ValueError as exc:
        return _error(str(exc))
    except (IndexError, KeyError, TypeError, ArithmeticError):
        return _error("The stored result is malformed and cannot be shown as a card. Run the query again.")
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
    loader = _Loader(context or {})
    try:
        left = await loader.load(spec.left_result_id)
        right = await loader.load(spec.right_result_id)
        controls = (
            await loader.load(spec.left_control_result_id) if spec.left_control_result_id else None,
            await loader.load(spec.right_control_result_id) if spec.right_control_result_id else None,
        )
        card, facts = build_compare_card(spec, left, right, controls)
    except ValueError as exc:
        return _error(str(exc))
    except (IndexError, KeyError, TypeError, ArithmeticError):
        return _error("The stored result is malformed and cannot be shown as a card. Run the query again.")
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
