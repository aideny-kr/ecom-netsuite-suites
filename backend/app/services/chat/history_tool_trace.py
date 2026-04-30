"""Render compact tool-call traces from prior assistant turns.

Why this module exists
----------------------
The orchestrator's history loader (``_chat_agentic`` in ``orchestrator.py``)
passes only ``ChatMessage.content`` to the LLM — the ``tool_calls`` column
is dropped entirely. That means follow-up turns in the same session can
see the *narrative* of a previous answer but not the *query pattern* that
produced it, so the agent often rediscovers (and fails to rediscover) the
same joins / tables / quirks every turn.

Olivia's 2026-04-09 session is the canonical example: Turn 2 solved a
ship-country report via ``JOIN transactionShippingAddress sa ON sa.nKey =
t.shippingAddress`` using the external MCP ``ns_runCustomSuiteQL`` tool.
Turn 3 (a simple "break out laptop and desktop qty" follow-up) reverted
to the local ``netsuite_suiteql`` tool, hit the same ``NOT_EXPOSED`` error
Turn 1 had already failed on, and hallucinated zero orders for the four
countries — directly contradicting Turn 2's own numeric answer.

What this module does
---------------------
``render_tool_trace(tool_calls)`` produces a compact, LLM-friendly block
that lists the prior turn's tool calls, highlights successes vs failures,
and surfaces the actual SQL (truncated) of successful queries so the next
turn can reuse proven patterns. The output is wrapped in a single
``<tool_trace>...</tool_trace>`` block and is designed to be appended to
the assistant message's content during history replay.

Budget
------
Each call is rendered on 1-2 lines. Successful SQL is truncated to ~400
chars. Typical trace for a 3-step turn is ~500-800 chars; for a 12-step
failing turn (Olivia Turn 1) it's ~1000-1500 chars. Well within the
savings vs. the hallucination-repair cost on a follow-up turn.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Max characters per SQL query in the trace. 400 is long enough to see the
# join pattern and WHERE clause, short enough that a 12-step trace stays
# under ~2 KB.
_MAX_SQL_CHARS = 400

# Anything longer than this on a single line gets ellipsised.
_MAX_LINE_CHARS = 500

_EXT_TOOL_PREFIX_RE = re.compile(r"^ext__[a-f0-9]+__")

# Patterns that identify "this was a failure" in a result_summary string.
# The backend's summariser already returns compact strings like
# "Returned 4 rows" for success or "NetSuite query failed: ..." for failure;
# we pattern-match against those.
_FAILURE_MARKERS = (
    "failed",
    "error",
    "NOT_EXPOSED",
    "not found",
    "disallowed",
    "timeout",
    "invalid",
    "denied",
)


def _strip_ext_prefix(tool_name: str) -> str:
    """Strip the ``ext__<hex>__`` prefix used for external MCP tools."""
    return _EXT_TOOL_PREFIX_RE.sub("", tool_name)


def _is_failure(result_summary: str) -> bool:
    if not result_summary:
        return False
    lower = result_summary.lower()
    return any(marker in lower for marker in _FAILURE_MARKERS)


def _normalise_sql(sql: str) -> str:
    """Collapse whitespace and truncate to ``_MAX_SQL_CHARS``."""
    if not sql:
        return ""
    one_line = re.sub(r"\s+", " ", sql).strip()
    if len(one_line) <= _MAX_SQL_CHARS:
        return one_line
    return one_line[: _MAX_SQL_CHARS - 1] + "…"


def _extract_sql(params: dict[str, Any]) -> str:
    """Pull the SQL query from a tool-call params dict.

    Local tool uses ``query``, external MCP uses ``sqlQuery``.
    """
    if not isinstance(params, dict):
        return ""
    return params.get("query") or params.get("sqlQuery") or ""


def _extract_failure_reason(result_summary: str) -> str:
    """Extract a short human-readable failure reason from a result summary.

    The backend summariser produces strings like:
        "NetSuite query failed: NetSuite API error 400: ..."
        "Query references disallowed tables: entityaddress..."
        "Tool execution exceeded 60-second timeout limit"

    We return a compact one-liner. Looks for key markers like NOT_EXPOSED,
    specific field names, etc.
    """
    if not result_summary:
        return "unknown error"

    # NOT_EXPOSED — NetSuite's "exists in the schema but not searchable"
    # error. Check this first because the surrounding text often also says
    # "not found" which would otherwise swallow it.
    if "NOT_EXPOSED" in result_summary:
        field_match = re.search(r"Field '([^']+)'", result_summary)
        if field_match:
            return f"{field_match.group(1)} NOT_EXPOSED"
        return "NOT_EXPOSED"

    # Field not found
    field_not_found = re.search(r"Field '([^']+)'[^.]*not found", result_summary)
    if field_not_found:
        return f"{field_not_found.group(1)} not found"

    # Disallowed tables
    disallowed = re.search(r"disallowed tables?: ([^.]+)", result_summary)
    if disallowed:
        return f"disallowed table {disallowed.group(1).strip()}"

    # Timeout
    if "timeout" in result_summary.lower():
        return "timeout"

    # Fallback — first 120 chars
    return result_summary.strip()[:120]


def _render_call(call: dict[str, Any]) -> list[str]:
    """Render a single tool call into 1-2 trace lines."""
    tool_name = _strip_ext_prefix(str(call.get("tool", "?")))
    step = call.get("step", "?")
    params = call.get("params") or {}
    result_summary = call.get("result_summary") or ""

    failed = _is_failure(result_summary)
    sql = _extract_sql(params)

    if failed:
        reason = _extract_failure_reason(result_summary)
        # Failures: single line, no SQL replay (agent already knows the SQL
        # failed — repeating it just wastes tokens).
        return [f"[step {step}] {tool_name} → FAILED: {reason}"]

    # Success path — one line header + optional SQL/params line
    header_parts: list[str] = [f"[step {step}] {tool_name}"]

    # Row count if available
    row_match = re.search(r"Returned (\d+) rows?", result_summary)
    if row_match:
        header_parts.append(f"→ OK ({row_match.group(1)} rows)")
    else:
        header_parts.append("→ OK")

    header = " ".join(header_parts)
    lines = [header]

    # For SQL tools, show the query (truncated)
    if sql:
        normalised = _normalise_sql(sql)
        lines.append(f"  SQL: {normalised}")
    else:
        # For non-SQL tools (ns_runReport, ns_getRecord, ns_getSuiteQLMetadata),
        # show a compact params summary so the agent can see WHAT was inspected.
        compact = _compact_params(params)
        if compact:
            lines.append(f"  params: {compact}")

    return lines


def _compact_params(params: dict[str, Any]) -> str:
    """Render non-SQL params in a compact one-liner."""
    if not isinstance(params, dict) or not params:
        return ""
    parts: list[str] = []
    for k, v in params.items():
        if k in ("query", "sqlQuery", "description", "user_question"):
            continue
        if isinstance(v, (dict, list)):
            value_str = json.dumps(v, default=str, separators=(",", ":"))
        else:
            value_str = str(v)
        parts.append(f"{k}={value_str}")
        if len(", ".join(parts)) > 200:
            break
    compact = ", ".join(parts)
    if len(compact) > 200:
        compact = compact[:199] + "…"
    return compact


def render_tool_trace(tool_calls: list[dict[str, Any]] | None) -> str:
    """Render a compact trace of prior tool calls for history replay.

    Args:
        tool_calls: The ``ChatMessage.tool_calls`` JSON column — a list of
            dicts with ``step``, ``tool``, ``params``, ``result_summary``,
            ``duration_ms`` keys. May be ``None`` or empty.

    Returns:
        A ``<tool_trace>...</tool_trace>`` block containing one entry per
        tool call, or the empty string if ``tool_calls`` is empty/None.
    """
    if not tool_calls:
        return ""

    lines: list[str] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        for line in _render_call(call):
            # Guard against any single line blowing up the budget
            if len(line) > _MAX_LINE_CHARS:
                line = line[: _MAX_LINE_CHARS - 1] + "…"
            lines.append(line)

    if not lines:
        return ""

    body = "\n".join(lines)
    return f"<tool_trace from previous turn>\n{body}\n</tool_trace>"


# ---------------------------------------------------------------------------
# History loading with tool-trace replay
# ---------------------------------------------------------------------------


# Max chars per clarification option field surfaced into history. The card's
# UI fields (title/rationale) are user-facing prose and rarely need more —
# 200 keeps a 3-option card under ~1.5 KB.
_MAX_CLARIFY_FIELD_CHARS = 200


def render_clarification_summary(structured_output: dict[str, Any] | None) -> str:
    """Render a compact summary of a clarification's options.

    Used to surface the chosen-option's full definition into chat history
    when the assistant message itself was persisted with empty ``content``
    (Plan Mode clarification turns intentionally suppress assistant prose —
    the card IS the message).

    Round 10 P2 Bug 1: without this, follow-up turns see the resume
    directive ``"Picked option B (source: netsuite)"`` plus an empty prior
    assistant message — so they cannot tell which definition was picked
    when two options share a source (fiscal Q1 vs calendar Q1, both
    NetSuite). Surfacing options as content lets the model interpret the
    directive correctly.

    Returns the empty string if ``structured_output`` is missing, malformed,
    or not a clarification.
    """
    if not isinstance(structured_output, dict):
        return ""
    if structured_output.get("type") != "clarification":
        return ""
    options = structured_output.get("options")
    if not isinstance(options, list) or not options:
        return ""

    lines: list[str] = ["Clarification offered:"]
    for opt in options:
        if not isinstance(opt, dict):
            continue
        opt_id = str(opt.get("id", "?"))[:4]
        title = str(opt.get("title", ""))[:_MAX_CLARIFY_FIELD_CHARS]
        source = str(opt.get("source", "?"))[:32]
        lines.append(f"  - Option {opt_id}: {title} (source: {source})")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def build_history_dicts(
    messages: list[dict[str, Any]],
    keep_recent: int,
    include_tool_trace: bool = True,
) -> tuple[list[dict[str, str]], int]:
    """Build the list of ``{role, content}`` dicts fed to the LLM.

    Args:
        messages: List of dicts representing ``ChatMessage`` rows, each with
            keys: ``role``, ``content``, ``content_summary``, ``tool_calls``,
            ``structured_output``. Only ``role`` and ``content`` are
            strictly required; the others default to empty.
        keep_recent: Number of most-recent messages kept verbatim (older
            assistant messages are replaced by their ``content_summary`` if
            available).
        include_tool_trace: When ``True`` (default), assistant messages in
            the kept-recent window that have ``tool_calls`` populated get a
            compact ``<tool_trace>`` block appended to their content. This
            is the fix for the Olivia tangent — see module docstring.

    Returns:
        ``(history_dicts, num_summarised)`` — the list of messages for the
        LLM and the count of older messages that were replaced with their
        summary.
    """
    history: list[dict[str, str]] = []
    summarised = 0

    if not messages:
        return history, 0

    total = len(messages)
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue

        is_recent = i >= total - keep_recent
        content = msg.get("content") or ""
        content_summary = msg.get("content_summary")
        tool_calls = msg.get("tool_calls")
        structured_output = msg.get("structured_output")

        # Round 10 P2 Bug 1: clarification turns persist with content="" so
        # the LLM cannot interpret a later "Picked option B" directive. If
        # this assistant message has empty content + a clarification
        # structured_output, synthesize a compact options summary as the
        # LLM-facing content. Skipped when the message already has prose
        # so we never overwrite real assistant content.
        if role == "assistant" and not content:
            clarify_summary = render_clarification_summary(structured_output)
            if clarify_summary:
                content = clarify_summary

        if is_recent or not content_summary:
            # Recent — keep full content
            final_content = content
            if include_tool_trace and is_recent and role == "assistant" and tool_calls:
                trace = render_tool_trace(tool_calls)
                if trace:
                    final_content = f"{content}\n\n{trace}" if content else trace
            history.append({"role": role, "content": final_content})
        else:
            history.append({"role": role, "content": content_summary})
            summarised += 1

    return history, summarised
