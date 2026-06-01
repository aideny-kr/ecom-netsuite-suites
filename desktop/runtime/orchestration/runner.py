"""Extraction-shaped runner: wraps Hermes `run_conversation` into typed events.

Transport-agnostic core (spec sections 3.2 / 4): it accepts an *injected* agent
and an `emit` callback, wires Hermes' streaming + tool-result callbacks, and
emits `text` / `data_table` / `done` events. It writes to NO transport (no
direct output, no cross-process channel) — the desktop adapter layer is the only
thing that serializes these events to a process boundary, so the later
`packages/agent` extraction relocates this module unchanged.

This MIRRORS the webapp's `_intercept_tool_result` -> typed-event mechanism
(`backend/app/services/chat/`); it does not reinvent the interception semantics.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from orchestration.events import DataTableEvent, DoneEvent, TextEvent

# Tool names whose `{columns, rows}` result is converted to a `data_table` event.
DATA_TABLE_TOOLS = ("sample_dataset",)

Emit = Callable[[Any], None]


def _tokens_used(result: Any) -> int:
    """Per-turn token count from a `run_conversation` result dict.

    Prefer `total_tokens` (authoritative on the Hermes side); fall back to
    `input_tokens + output_tokens`; default 0 so `done` never carries null.
    Kept local to this package to stay decoupled from the desktop adapter.
    """
    if not isinstance(result, dict):
        return 0
    total = result.get("total_tokens")
    if isinstance(total, int):
        return total
    inp = result.get("input_tokens")
    out = result.get("output_tokens")
    inp_n = inp if isinstance(inp, int) else 0
    out_n = out if isinstance(out, int) else 0
    return inp_n + out_n


def _parse_tool_result(tool_result: Any) -> Any:
    """Hermes passes the tool handler's return as `tool_result` (a JSON string).

    Tolerant: accept an already-parsed dict, JSON-decode a string, and return
    None on anything malformed — a bad tool result is skipped rather than
    crashing the turn (the assistant's text reply still streams).
    """
    if isinstance(tool_result, dict):
        return tool_result
    if isinstance(tool_result, str):
        try:
            return json.loads(tool_result)
        except json.JSONDecodeError:
            return None
    return None


def run_agent_stream(
    query: str,
    emit: Emit,
    *,
    agent: Any,
    data_table_tools: tuple[str, ...] = DATA_TABLE_TOOLS,
) -> Any:
    """Drive one turn of `agent.run_conversation(query)`, emitting typed events.

    Args:
        query: the user prompt.
        emit: a sink called once per event, in order (text* -> data_table -> ...
            -> done). Transport-agnostic — the adapter passes a serializing
            writer; tests pass `list.append`.
        agent: a Hermes-`AIAgent`-shaped object. The runner sets its
            `stream_delta_callback` and `tool_complete_callback` attributes
            (settable per run on a reused agent) and calls `run_conversation`.
        data_table_tools: tool names whose result is converted to a data_table.

    Returns the underlying `run_conversation` result dict.
    """

    def on_text(text: Any) -> None:
        # Hermes fires a terminal `None` delta at end-of-stream; skip falsy text.
        if text:
            emit(TextEvent(content=text))

    def on_tool(tool_call_id: str, tool_name: str, tool_args: Any, tool_result: Any) -> None:
        if tool_name not in data_table_tools:
            return
        parsed = _parse_tool_result(tool_result)
        # Require list-typed columns AND rows — a malformed tool result is
        # skipped (no data_table) rather than crashing the turn; the assistant's
        # text reply still streams.
        if (
            isinstance(parsed, dict)
            and isinstance(parsed.get("columns"), list)
            and isinstance(parsed.get("rows"), list)
        ):
            emit(DataTableEvent.from_tool_result(parsed))

    agent.stream_delta_callback = on_text
    agent.tool_complete_callback = on_tool

    result = agent.run_conversation(query)

    emit(DoneEvent(tokens_used=_tokens_used(result)))
    return result
