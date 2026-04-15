"""Write confirmation service — builds and validates HITL confirmation payloads
for NetSuite AI agent write operations.

Consumers:
- Chat orchestrator: calls ``build_confirmation_payload`` before any mutation
  tool is executed, then emits the payload as an SSE ``confirmation_required``
  event so the frontend can show a confirmation dialog.
- Chat runs API: calls ``validate_and_extract_confirmation`` on the user's
  ``write_confirm`` (approve/reject) POST to verify the token before executing
  the deferred tool call.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel

from app.services.chat.mutation_guard import (
    generate_confirmation_token,
    is_record_type_allowed,
    verify_confirmation_token,
)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class WriteConfirmationPayload(BaseModel):
    """Describes a pending AI-initiated write operation awaiting human approval.

    Sent to the frontend as ``structured_output`` inside an SSE
    ``confirmation_required`` event.
    """

    type: Literal["write_confirmation"] = "write_confirmation"
    mutation_type: Literal["create", "update", "delete", "upsert"]
    record_type: str
    record_id: str | None = None
    proposed_fields: dict[str, Any]
    current_record: dict[str, Any] | None = None
    tool_name: str
    tool_input: dict[str, Any]
    confirmation_token: str
    status: Literal["pending", "approved", "rejected"] = "pending"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_payload_json(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Canonical JSON representation used for HMAC signing.

    ``sort_keys=True`` and ``default=str`` ensure deterministic output across
    Python versions regardless of dict insertion order.
    """
    return json.dumps(
        {"tool_name": tool_name, "tool_input": tool_input},
        sort_keys=True,
        default=str,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_confirmation_payload(
    mutation_type: str,
    record_type: str,
    tool_name: str,
    tool_input: dict[str, Any],
    session_id: str,
    current_record: dict[str, Any] | None = None,
) -> WriteConfirmationPayload | None:
    """Build a ``WriteConfirmationPayload`` for a pending write operation.

    Returns ``None`` if *record_type* is not on the mutation allowlist (either
    explicitly blocked or simply unknown — deny by default).

    Parameters
    ----------
    mutation_type:
        One of ``"create"``, ``"update"``, ``"delete"``, or ``"upsert"``.
    record_type:
        The NetSuite record type (e.g. ``"salesOrder"``, ``"invoice"``).
    tool_name:
        The full qualified external-MCP tool name
        (e.g. ``"ext__<32hex>__ns_createRecord"``).
    tool_input:
        The raw tool input dict as received from the LLM.
    session_id:
        The current chat session ID — bound into the HMAC token so tokens
        cannot be replayed across sessions.
    current_record:
        Optional snapshot of the record's current state (used for before/after
        diff display in the frontend confirmation dialog).
    """
    if not is_record_type_allowed(record_type):
        return None

    body: dict[str, Any] = tool_input.get("body", {}) or {}
    proposed_fields: dict[str, Any] = body

    # Prefer top-level ``id`` (update/delete) then fall back to ``body.id``
    record_id: str | None = tool_input.get("id") or body.get("id") or None
    if record_id is not None:
        record_id = str(record_id)

    payload_json = _build_payload_json(tool_name, tool_input)
    confirmation_token = generate_confirmation_token(session_id, payload_json)

    return WriteConfirmationPayload(
        mutation_type=mutation_type,
        record_type=record_type,
        record_id=record_id,
        proposed_fields=proposed_fields,
        current_record=current_record,
        tool_name=tool_name,
        tool_input=tool_input,
        confirmation_token=confirmation_token,
    )


def validate_and_extract_confirmation(
    structured_output: dict[str, Any],
    session_id: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Validate a confirmation payload received from the frontend.

    Rebuilds the original ``payload_json`` from the ``tool_name`` and
    ``tool_input`` fields in *structured_output* and verifies the HMAC token.

    Returns
    -------
    ``(is_valid, tool_name, tool_input)``
        ``is_valid`` is ``True`` only when the token matches.  The caller
        should ignore ``tool_name`` and ``tool_input`` when ``is_valid`` is
        ``False``.
    """
    token: str = structured_output.get("confirmation_token", "")
    tool_name: str = structured_output.get("tool_name", "")
    tool_input: dict[str, Any] = structured_output.get("tool_input", {})

    payload_json = _build_payload_json(tool_name, tool_input)
    is_valid = verify_confirmation_token(token, session_id, payload_json)

    return is_valid, tool_name, tool_input
