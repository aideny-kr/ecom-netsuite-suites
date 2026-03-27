"""Tool for agents to retrieve cached results from previous queries in the conversation."""

import json

from app.services.chat.result_cache import get_latest_result, get_result_by_message

TOOL_DEFINITION = {
    "name": "reference_previous_result",
    "description": (
        "Retrieve data from a previous query result in this conversation. "
        "Use this instead of re-querying when the user asks to chart, pivot, "
        "export, or transform data they already see. "
        "Returns column names and rows (up to 50) suitable for charting."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "Optional message ID to reference a specific result. Omit for most recent result.",
            },
        },
        "required": [],
    },
}


async def execute_reference_previous_result(
    conversation_id: str,
    message_id: str | None = None,
) -> str:
    """Execute the reference_previous_result tool."""
    if message_id:
        result = await get_result_by_message(conversation_id, message_id)
    else:
        result = await get_latest_result(conversation_id)

    if not result:
        return json.dumps(
            {
                "success": False,
                "error": (
                    "No cached result found. The data may have expired (30-min TTL) "
                    "or no query was run recently. Please re-query to get fresh data."
                ),
            }
        )

    return json.dumps(
        {
            "success": True,
            "result_type": result.result_type,
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "summary": result.summary,
            "query_text": result.query_text,
            "note": "This is cached data from a previous query. Use it for charting, pivoting, or transforming — do NOT re-query.",
        },
        default=str,
    )
