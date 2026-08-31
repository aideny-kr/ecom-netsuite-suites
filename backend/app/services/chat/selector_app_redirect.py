"""Redirect ``ns_selector_app`` to the slot mechanism that actually works here.

THE PROBLEM, measured rather than assumed. NetSuite's MCP surface offers
``ns_selector_app``, which opens a record picker inside NetSuite's own UI. To a
model deciding "the user must choose a subsidiary" it is the obviously correct
tool. In this product it is a dead end: nothing in the frontend or backend
renders it, so the model reports "I've opened the subsidiary selector for you"
and there is no selector anywhere — an asserted affordance that does not exist,
which is worse for an operator than being asked in prose.

Across five live attempts on staging (2026-08-26) at creating a customer
without naming a subsidiary, THREE ended in ``ns_selector_app`` — two of those
under prompts that explicitly said to use ``ask_user`` and not to answer in
chat — and two ended in prose. None reached the write-confirmation card's slot
form, which has been built and shipped throughout.

THE FIX WORKS WITH THE MODEL, NOT AGAINST IT. The intent is already right: it
wants a human to pick from a list, which is exactly what an editable slot is.
It is calling the wrong door. So the call is intercepted and answered with the
right one, as code at a choke point — three attempts at prompt wording have
been ignored, and `.claude/rules/agent-graph.md` is explicit that guardrails
are code at the choke point, never prompt prose.

Deliberately NOT executed: the call never reaches NetSuite. Opening a picker
nobody can see has no upside, and skipping it also saves a live MCP round trip.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["SELECTOR_TOOL", "build_selector_redirect", "is_selector_app_call"]

SELECTOR_TOOL = "ns_selector_app"


def is_selector_app_call(tool_name: str) -> bool:
    """True for ``ns_selector_app`` on any connector.

    Deliberately narrow: matched on the bare tool name so the external
    ``ext__<connector>__<tool>`` form works for every connector, and nothing
    else is affected — least of all ``ns_getRecordTypeMetadata`` /
    ``ns_getSubsidiaries``, which the write loop itself depends on.
    """
    if not tool_name or not isinstance(tool_name, str):
        return False
    # Canonical parser rather than a private rsplit — it owns the
    # `ext__<connector>__<tool>` format. Imported here, not at module scope,
    # because `tools` imports this module's siblings.
    from app.services.chat.tools import parse_external_tool_name

    parsed = parse_external_tool_name(tool_name)
    return (parsed[1] if parsed else tool_name) == SELECTOR_TOOL


def build_selector_redirect(tool_input: Any, *, mutation_record_type: str | None) -> str:
    """The tool result handed back instead of opening a picker.

    Must do two things, and the second is the one that is easy to forget: name
    the exact call to make instead, AND contradict the belief that a selector
    opened. Without the contradiction the model narrates "I've opened the
    selector" regardless of what the result said, because that is what it
    believed it was doing.
    """
    field = ""
    if isinstance(tool_input, dict):
        raw = tool_input.get("recordType") or tool_input.get("record_type") or ""
        field = str(raw).strip() if raw is not None else ""

    write_tool = "ns_createRecord"
    target = mutation_record_type or "the record"
    field_label = field or "that field"
    ask_list = f'["{field}"]' if field else '["<field name>"]'

    return json.dumps(
        {
            "selector_unavailable": True,
            "field": field,
            # Stated flatly and first: no picker was shown. The model must not
            # tell the user to go look at one.
            "detail": (
                "No selector was opened and nothing is shown to the user. This client cannot "
                f"render {SELECTOR_TOOL}; it is a NetSuite-hosted UI with no surface here."
            ),
            "instruction": (
                f"To let the user choose {field_label}, do NOT open a selector and do NOT ask in chat. "
                f"Call {write_tool} for {target} with the fields you already know, and add "
                f'"ask_user": {ask_list} to that same tool call. The server then fetches the real '
                "options itself and renders them as a dropdown on the confirmation card the user "
                "already has to approve. Send the field NAME only — never values or your own option list."
            ),
        }
    )
