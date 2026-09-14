"""Trusted Metabase result metadata for deterministic, source-preserving pivots.

Only the dispatcher binds an endpoint/connector. Remote responses cannot supply
that authority. Preserve native data for the existing numeric-evidence path.
"""

from copy import deepcopy


def bind_result(result: dict, params: dict, connector) -> dict:
    result = {k: v for k, v in result.items() if k != "metabase_source"}
    data = result.get("data")
    if result.get("error") or result.get("isError") or result.get("success") is False:
        return result
    if not isinstance(data, dict):
        return result
    cols, rows = data.get("cols"), data.get("rows")
    if not isinstance(cols, list) or not cols or not all(isinstance(c, dict) for c in cols):
        return result
    if not isinstance(rows, list) or any(not isinstance(r, list) or len(r) != len(cols) for r in rows):
        return result
    query = params.get("query")
    if not isinstance(query, dict):
        query = result.get("json_query")
    stages = query.get("stages") if isinstance(query, dict) else None
    stage = stages[-1] if isinstance(stages, list) and stages and isinstance(stages[-1], dict) else {}
    complete = result.get("status") == "completed" and not any(
        obj.get(key)
        for obj in (result, data)
        for key in ("continuation_token", "truncated", "rows_truncated", "has_more")
    )
    for obj in (result, data):
        for key in ("row_count", "rowCount"):
            count = obj.get(key)
            if count is not None and (type(count) is not int or count != len(rows)):
                complete = False
    for limit in (stage.get("limit"), params.get("limit"), params.get("page_size")):
        if type(limit) is int and len(rows) >= limit:
            complete = False
    return {
        **result,
        "columns": [str(c.get("display_name") or c.get("name") or "Value") for c in cols],
        "rows": rows,
        "row_count": len(rows),
        "truncated": not complete,
        "source_kind": "metabase",
        "metabase_source": {
            "connector_id": str(connector.id),
            "server_url": connector.server_url,
            "query": deepcopy(query),
            "column_sources": [c.get("source") for c in cols],
            "column_names": [c.get("name") for c in cols],
            "complete": complete,
        },
    }


def is_bound_table(value) -> bool:
    return (
        isinstance(value, dict)
        and value.get("source_kind") == "metabase"
        and isinstance(value.get("metabase_source"), dict)
        and isinstance(value.get("columns"), list)
        and isinstance(value.get("rows"), list)
    )


async def nonstream_interceptor(db, tenant_id, session_id, current_calls):
    """Use the same result IDs/full storage on the non-streaming agent path."""
    from app.services.chat.orchestrator import _make_tool_interceptor, _write_full_payload_sidecar
    from app.services.chat.tool_call_results import count_payload_bearing_tool_calls, load_conversation_tool_messages

    previous = await load_conversation_tool_messages(db, session_id, tenant_id)
    count = count_payload_bearing_tool_calls([*previous, {"role": "assistant", "tool_calls": current_calls}])

    def cache(tool_name, event_type, event_data, result_id, params, result_str, full_payload):
        if result_id and full_payload is not None:
            _write_full_payload_sidecar(str(session_id), result_id, tool_name, params, full_payload)

    return _make_tool_interceptor(cache_callback=cache, start_count=count)
