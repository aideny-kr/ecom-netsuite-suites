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

    if parsed:
        error_message = _extract_error_message(parsed)
        if error_message:
            return error_message[:500]

        # workspace_propose_patch's result carries the changeset_id the frontend
        # needs to render ChangeProposalCard's Approve/Apply buttons. Don't
        # collapse it to "Returned 1 row" — return an allowlisted JSON payload
        # so parseResult() in change-proposal-card.tsx can extract changeset_id.
        #
        # CRITICAL: the raw propose_patch result includes diff_preview with up
        # to 32KB of original_content + modified_content (see workspace_service
        # .propose_patch). SuiteScripts can contain credentials, tokens,
        # internal IDs, customer data, or business logic. Persisting that into
        # ChatMessage.tool_calls — which is replayed into LLM history and
        # shipped to the frontend — would leak file contents downstream.
        # Allowlist action-relevant fields only; the frontend already has the
        # diff in step.params.unified_diff.
        if tool_name == "workspace_propose_patch" and parsed.get("changeset_id"):
            return json.dumps(
                {
                    "changeset_id": parsed["changeset_id"],
                    "patch_id": parsed.get("patch_id", ""),
                    "operation": parsed.get("operation", "modify"),
                    "diff_status": parsed.get("diff_status", "unknown"),
                    "risk_summary": parsed.get("risk_summary", ""),
                }
            )

    # Try to compute a row count from any known shape
    row_count: int | None = None
    if isinstance(parsed, dict):
        row_count = parsed.get("row_count") if isinstance(parsed.get("row_count"), int) else None
        if row_count is None and isinstance(parsed.get("count"), int):
            row_count = parsed["count"]
        if row_count is None:
            for key in ("rows", "items"):
                collection = parsed.get(key)
                if isinstance(collection, list):
                    row_count = len(collection)
                    break

    # Top-level list (e.g. ns_listAllReports returns [...])
    if row_count is None and isinstance(result_str, str):
        try:
            top_level = json.loads(result_str)
            if isinstance(top_level, list):
                row_count = len(top_level)
        except (json.JSONDecodeError, TypeError):
            pass

    if row_count is None:
        return result_str[:500]

    suffix = ""
    if isinstance(parsed, dict) and (parsed.get("truncated") or parsed.get("rows_truncated")):
        suffix = " (truncated)"
    if row_count == 0:
        return f"No rows returned{suffix}"
    return f"Returned {row_count} row{'s' if row_count != 1 else ''}{suffix}"


def _extract_items_as_table(parsed: dict[str, Any] | list) -> tuple[list[str], list[list]] | None:
    """Extract columns/rows from a list-of-dicts response (MCP SuiteQL, saved searches, etc.).

    Handles:
      - {"items": [{...}, ...]} — ns_runCustomSuiteQL, ns_runSavedSearch
      - {"data": [{...}, ...]} — external MCP ns_runCustomSuiteQL (chat-orchestration
        rule #3: external MCP returns ``{"data": [{col: val}], ...}``, NOT columns/rows).
        The interceptor's data_table branch (orchestrator._intercept_tool_result) treats
        this top-level ``data`` key as a data result and stamps a result_id, so the
        payload extractor MUST recognize the same shape — otherwise the SINGLE
        id-assignment criterion (payload non-None) never fires for this (most common
        NetSuite) data source and report.compose can't resolve the stamped id.
      - [{...}, ...] — ns_listAllReports, ns_listSavedSearches (top-level list)
      - {"reportData": {...}} — ns_runReport (hierarchical, handled separately)
    """
    items: list[dict] | None = None

    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict):
        # Prefer "items"; fall back to the external-MCP "data" key (same shape).
        items_val = parsed.get("items")
        if not isinstance(items_val, list):
            items_val = parsed.get("data")
        if isinstance(items_val, list):
            items = items_val

    if not items or not isinstance(items[0], dict):
        return None

    # Derive columns from all items (union of keys, preserving first-seen order)
    seen: set[str] = set()
    columns: list[str] = []
    for item in items:
        for key in item:
            if key not in seen:
                seen.add(key)
                columns.append(key)

    # Build rows aligned to columns
    rows = [[item.get(col) for col in columns] for item in items]
    return columns, rows


def _extract_report_data_as_table(report_data: dict) -> tuple[list[str], list[list]] | None:
    """Flatten ns_runReport hierarchical reportData into columns/rows."""
    if not isinstance(report_data, dict) or not report_data:
        return None

    columns = ["row", "account", "amount"]
    rows: list[list] = []

    for _key, entry in sorted(report_data.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
        if not isinstance(entry, dict):
            continue
        label = entry.get("value") or entry.get("label") or ""
        # Get amount from summaryLineValues or detailLineValues
        amount = None
        for vals_key in ("summaryLineValues", "detailLineValues"):
            vals = entry.get(vals_key)
            if isinstance(vals, list) and vals:
                first = vals[0]
                if isinstance(first, dict):
                    amount = first.get("Amount") or first.get("amount")
                    break
        is_detail = entry.get("isDetailLine", False)
        row_type = "detail" if is_detail else "section"
        if label or amount is not None:
            rows.append([row_type, str(label), amount])

    return (columns, rows) if rows else None


def extract_result_payload(tool_name: str, params: dict[str, Any], result_str: str) -> dict[str, Any] | None:
    """Attach structured query results for UI rendering when available.

    Handles local netsuite_suiteql (columns/rows format) and external MCP tools
    (items list-of-dicts, reportData hierarchical).
    """
    parsed = parse_tool_result_value(result_str)

    # Handle top-level list (ns_listAllReports, ns_listSavedSearches)
    if not parsed and isinstance(result_str, str):
        try:
            top_level = json.loads(result_str)
            if isinstance(top_level, list) and top_level and isinstance(top_level[0], dict):
                parsed = top_level  # type: ignore[assignment]
        except (json.JSONDecodeError, TypeError):
            pass

    if not parsed:
        return None

    if isinstance(parsed, dict) and _extract_error_message(parsed):
        return None

    # --- Path 1: Already has columns + rows (local netsuite_suiteql) ---
    if isinstance(parsed, dict):
        columns = parsed.get("columns")
        rows = parsed.get("rows")
        if isinstance(columns, list) and isinstance(rows, list):
            row_count = parsed.get("row_count")
            if not isinstance(row_count, int):
                row_count = len(rows)
            query = parsed.get("query")
            if not isinstance(query, str):
                query = params.get("query", params.get("sqlQuery", ""))
            limit = parsed.get("limit")
            if not isinstance(limit, int):
                limit_param = params.get("limit")
                limit = limit_param if isinstance(limit_param, int) else len(rows)
            entry: dict[str, Any] = {
                "kind": "table",
                "columns": columns,
                "rows": rows,
                "row_count": row_count,
                "truncated": bool(parsed.get("truncated") or parsed.get("rows_truncated")),
                "query": query,
                "limit": limit,
            }
            # M4: For metric payloads, pass through source_kind so
            # _compute_source_pin_update can distinguish BigQuery vs SuiteQL
            # metrics without mis-pinning NetSuite for a BigQuery metric.
            # Only set when the payload carries the flag (metric trust boundary).
            if parsed.get("suppress_llm_value") is True and "source_kind" in parsed:
                entry["source_kind"] = parsed["source_kind"]
            # Gate B (report provenance): a blessed-metric payload carries
            # definition_version (and source_kind) at the top level — preserve them
            # on the frozen entry so report.compose's metric_headline can attribute
            # the number to its definition version (the §10 audit-citation source).
            # Additive only: never remove keys; copy when present.
            if isinstance(parsed.get("definition_version"), int):
                entry["definition_version"] = parsed["definition_version"]
            if "source_kind" not in entry and "source_kind" in parsed:
                entry["source_kind"] = parsed["source_kind"]
            return entry

    # --- Path 2: reportData (ns_runReport) ---
    if isinstance(parsed, dict):
        report_data = parsed.get("reportData")
        if isinstance(report_data, dict):
            result = _extract_report_data_as_table(report_data)
            if result:
                columns, rows = result
                return {
                    "kind": "table",
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                    "truncated": False,
                    "query": f"ns_runReport(reportId={params.get('reportId', '?')})",
                    "limit": len(rows),
                }

    # --- Path 3: items list-of-dicts (MCP SuiteQL, saved searches) ---
    result = _extract_items_as_table(parsed)
    if result:
        columns, rows = result
        row_count = len(rows)
        query = params.get("sqlQuery", params.get("query", ""))
        return {
            "kind": "table",
            "columns": columns,
            "rows": rows,
            "row_count": row_count,
            "truncated": False,
            "query": query,
            "limit": row_count,
        }


def _tool_calls_of(message: Any) -> list[dict[str, Any]]:
    """Normalize a persisted assistant message's tool_calls into a list of dicts.

    Accepts either a plain dict (in-memory turn payload) or a ChatMessage ORM row.
    Tolerates None / non-list shapes.
    """
    if isinstance(message, dict):
        calls = message.get("tool_calls")
    else:
        calls = getattr(message, "tool_calls", None)
    if not isinstance(calls, list):
        return []
    return [c for c in calls if isinstance(c, dict)]


def count_payload_bearing_tool_calls(messages: list[Any]) -> int:
    """Count persisted tool_calls carrying a ``result_payload`` dict across the
    given (already-loaded) conversation messages.

    This is the CONVERSATION-ORDINAL counter seed (findings #5/#9/#13): the in-turn
    interceptor numbers THIS turn's data results r(K+1), r(K+2), ... where K is this
    count over the prior conversation history — so the in-turn ids and the persisted
    FALLBACK ids (``resolve_payload_from_messages``, which numbers the SAME population
    1..K) live in ONE id space, never colliding/overwriting across turns.

    Uses the EXACT same criterion as the fallback resolver — a tool_call whose
    ``result_payload`` is a dict — so the two numbering schemes can never drift.
    """
    count = 0
    for message in messages:
        for call in _tool_calls_of(message):
            if isinstance(call.get("result_payload"), dict):
                count += 1
    return count


def resolve_payload_from_messages(messages: list[Any], result_id: str) -> dict[str, Any]:
    """Resolve a result_id to its FULL, uncapped frozen payload — the CROSS-TURN /
    REGENERATION FALLBACK path (§16.1 fix).

    NOTE (gate cluster A): the PRIMARY same-turn resolution is the eager in-turn
    full-payload sidecar (``result_cache.get_full_payload``), which ``report.compose``
    consults FIRST. This persisted-message walk is only reached on a sidecar miss —
    i.e. a LATER turn (or a report regeneration) composing over results that were
    persisted by an EARLIER turn's assistant ChatMessage. The current turn's
    assistant message is not persisted until AFTER the agent loop, so this path
    cannot (and is not meant to) see THIS turn's just-computed results.

    Walks the assistant messages' ``tool_calls[].result_payload`` (built by
    ``extract_result_payload`` — uncapped, NOT the 50-row Redis result cache) and
    returns the payload whose ``result_id`` matches, or whose positional id
    (``r1``, ``r2``, ...) matches when the call carries no explicit ``result_id``.

    Raises ``KeyError`` when no tool call with a usable ``result_payload`` matches.
    """
    positional = 0
    fallback: dict[str, Any] | None = None
    fallback_key: str | None = None
    for message in messages:
        for call in _tool_calls_of(message):
            payload = call.get("result_payload")
            if not isinstance(payload, dict):
                continue
            positional += 1
            synthetic_id = f"r{positional}"
            explicit_id = call.get("result_id")
            if explicit_id == result_id:
                return payload
            if synthetic_id == result_id:
                # Remember positional match but prefer an explicit-id match if one
                # appears later in the turn (explicit ids win over positional).
                if fallback is None:
                    fallback, fallback_key = payload, synthetic_id
    if fallback is not None and fallback_key == result_id:
        return fallback
    raise KeyError(result_id)


async def load_conversation_tool_messages(db: Any, conversation_id: Any, tenant_id: Any) -> list[Any]:
    """Load the assistant messages (with their full ``tool_calls``) for a session,
    newest turns last.

    ``tenant_id`` is REQUIRED (no default) — this is a defense-in-depth tenant filter.
    The chat tool-execution path does not call ``set_tenant_context``, so RLS may not be
    enforced on this session; we therefore add an explicit
    ``ChatMessage.tenant_id == tenant_id`` predicate rather than relying on RLS alone.
    Keeping the parameter required ensures a future caller cannot silently forget it.
    """
    from sqlalchemy import select

    from app.models.chat import ChatMessage

    if conversation_id is None:
        return []
    rows = (
        (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == conversation_id)
                .where(ChatMessage.tenant_id == tenant_id)
                .where(ChatMessage.role == "assistant")
                .order_by(ChatMessage.created_at, ChatMessage.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


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


# ---------------------------------------------------------------------------
# Distinct value extraction — prevents LLM from building IN(...) from memory
# ---------------------------------------------------------------------------

import re

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_NUMERIC_RE = re.compile(r"^-?\d+\.?\d*$")
_MAX_DISTINCT = 30  # Skip high-cardinality columns


def extract_distinct_values(result: Any) -> dict[str, list[str]]:
    """Extract distinct string values from categorical columns in a SuiteQL result.

    Returns {column_name: [sorted distinct values]} for columns that are:
    - String-typed (not all numeric, not date-like)
    - Low cardinality (≤ 30 distinct values)
    - Have 2+ distinct values (single-value columns aren't useful)

    Used to inject exact database values into follow-up prompts so the LLM
    doesn't reconstruct value lists from memory (dropping variants).
    """
    if not isinstance(result, dict):
        return {}

    columns = result.get("columns", [])
    rows = result.get("rows", [])

    if not columns or not rows or len(rows) < 2:
        return {}

    distinct: dict[str, set[str]] = {col: set() for col in columns}

    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue
        for i, val in enumerate(row):
            if i < len(columns) and val is not None:
                distinct[columns[i]].add(str(val))

    output: dict[str, list[str]] = {}
    for col, vals in distinct.items():
        if len(vals) < 2 or len(vals) > _MAX_DISTINCT:
            continue
        # Skip numeric columns
        if all(_NUMERIC_RE.match(v) for v in vals):
            continue
        # Skip date columns
        if all(_DATE_RE.match(v) for v in vals):
            continue
        output[col] = sorted(vals)

    return output


def append_distinct_values(result_str: str) -> str:
    """Append _distinct_values to a SuiteQL result JSON string.

    If the result has categorical columns with ≤ 30 distinct values,
    appends them as a _distinct_values key so the LLM can use exact
    values for follow-up CASE WHEN pivots.

    Returns the original string unchanged if no values to add.
    """
    try:
        parsed = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return result_str

    if not isinstance(parsed, dict):
        return result_str

    rows = parsed.get("rows", [])
    if not isinstance(rows, list) or len(rows) < 2:
        return result_str

    values = extract_distinct_values(parsed)
    if not values:
        return result_str

    parsed["_distinct_values"] = values
    return json.dumps(parsed, default=str)
