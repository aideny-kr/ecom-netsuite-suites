from __future__ import annotations

import json
from typing import Any


def parse_tool_result_value(result_value: Any) -> dict[str, Any]:
    """Best-effort parse of a tool result payload or summary string."""
    if isinstance(result_value, dict):
        return result_value
    if not isinstance(result_value, str):
        return {}
    try:
        parsed = json.loads(result_value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def get_tool_call_result_data(tool_call: dict[str, Any]) -> dict[str, Any]:
    """Return the most structured result representation available for a tool call."""
    result_payload = tool_call.get("result_payload")
    if isinstance(result_payload, dict):
        return result_payload
    return parse_tool_result_value(tool_call.get("result_summary"))


def tool_call_had_error(tool_call: dict[str, Any]) -> bool:
    data = get_tool_call_result_data(tool_call)
    error = data.get("error")
    return error is True or (isinstance(error, str) and bool(error.strip()))


def tool_call_row_count(tool_call: dict[str, Any]) -> int:
    data = get_tool_call_result_data(tool_call)
    row_count = data.get("row_count")
    if isinstance(row_count, int):
        return row_count

    rows = data.get("rows")
    if isinstance(rows, list):
        return len(rows)

    items = data.get("items")
    if isinstance(items, list):
        return len(items)

    return 0


def summarize_tool_result(tool_name: str, result_str: str) -> str:
    """Build a compact user-facing summary for persisted tool call logs."""
    parsed = parse_tool_result_value(result_str)

    if tool_name != "netsuite_suiteql":
        return result_str[:500]

    if not parsed:
        return result_str[:500]

    error_message = _extract_error_message(parsed)
    if error_message:
        return error_message[:500]

    row_count = parsed.get("row_count")
    if not isinstance(row_count, int):
        rows = parsed.get("rows")
        row_count = len(rows) if isinstance(rows, list) else 0

    suffix = " (truncated)" if parsed.get("truncated") or parsed.get("rows_truncated") else ""
    if row_count == 0:
        return f"No rows returned{suffix}"
    return f"Returned {row_count} row{'s' if row_count != 1 else ''}{suffix}"


def extract_result_payload(tool_name: str, params: dict[str, Any], result_str: str) -> dict[str, Any] | None:
    """Attach structured query results for UI rendering when available."""
    if tool_name != "netsuite_suiteql":
        return None

    parsed = parse_tool_result_value(result_str)
    if not parsed or _extract_error_message(parsed):
        return None

    columns = parsed.get("columns")
    rows = parsed.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return None

    row_count = parsed.get("row_count")
    if not isinstance(row_count, int):
        row_count = len(rows)

    query = parsed.get("query")
    if not isinstance(query, str):
        query = params.get("query", "")

    limit = parsed.get("limit")
    if not isinstance(limit, int):
        limit_param = params.get("limit")
        limit = limit_param if isinstance(limit_param, int) else len(rows)

    return {
        "kind": "table",
        "columns": columns,
        "rows": rows,
        "row_count": row_count,
        "truncated": bool(parsed.get("truncated") or parsed.get("rows_truncated")),
        "query": query,
        "limit": limit,
    }


def build_tool_call_log_entry(
    *,
    step: int,
    tool_name: str,
    params: dict[str, Any],
    result_str: str,
    duration_ms: int,
    agent_name: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "step": step,
        "tool": tool_name,
        "params": params,
        "result_summary": summarize_tool_result(tool_name, result_str),
        "duration_ms": duration_ms,
    }
    if agent_name:
        entry["agent"] = agent_name

    result_payload = extract_result_payload(tool_name, params, result_str)
    if result_payload is not None:
        entry["result_payload"] = result_payload

    return entry


def _extract_error_message(parsed: dict[str, Any]) -> str | None:
    error = parsed.get("error")
    if error is True:
        for key in ("message", "detail", "error_message"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return "Query failed"

    if isinstance(error, str) and error.strip():
        return error

    return None
