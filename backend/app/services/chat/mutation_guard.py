"""Mutation guard — detects write-path MCP tools and generates HMAC tokens
for human-in-the-loop write confirmation.

External MCP tools follow the naming scheme:
    ext__<32 hex chars>__<tool_name>

Mutation tools are those whose raw name (after stripping the ext__ prefix)
is one of the four write verbs understood by the Oracle NetSuite MCP server.
"""

from __future__ import annotations

import hashlib
import hmac

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Exact raw (unqualified) names that represent write operations.
_MUTATION_TOOL_NAMES: dict[str, str] = {
    "ns_createRecord": "create",
    "ns_updateRecord": "update",
    "ns_deleteRecord": "delete",
    "ns_upsertRecord": "upsert",
}

# Record types that are safe to create/update/delete via AI-initiated flows.
# Record types that must NEVER be mutated by the agent — system/security records.
# Everything else is allowed (HITL confirmation is the safety layer).
_BLOCKED_RECORD_TYPES: frozenset[str] = frozenset(
    {
        "employee",
        "role",
        "subsidiary",
        "department",
        "classification",
        "location",
        "account",
        "accountingPeriod",
        "customRecordType",
        "script",
        "workflow",
        "integration",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_tool_name(tool_name: str) -> str | None:
    """Return the unqualified tool name if tool_name is a valid ext__ tool,
    otherwise return None.  Delegates to the canonical parser in tools.py.
    """
    from app.services.chat.tools import parse_external_tool_name

    parsed = parse_external_tool_name(tool_name)
    return parsed[1] if parsed else None


def classify_mutation(tool_name: str) -> str | None:
    """Return the mutation verb ("create", "update", "delete", "upsert")
    for a mutation tool, or None if the tool is not a mutation.

    Single-pass: parses the tool name once and looks up the verb.
    Prefer this over calling ``is_mutation_tool`` + ``get_mutation_type``
    separately to avoid a redundant parse.
    """
    raw = _raw_tool_name(tool_name)
    if raw is None:
        return None
    return _MUTATION_TOOL_NAMES.get(raw)


def is_mutation_tool(tool_name: str) -> bool:
    """Return True if *tool_name* represents an external MCP write operation."""
    return classify_mutation(tool_name) is not None


def get_mutation_type(tool_name: str) -> str | None:
    """Return the mutation verb for a mutation tool, or None.

    Alias for ``classify_mutation`` — kept for backward compatibility.
    """
    return classify_mutation(tool_name)


def is_record_type_allowed(record_type: str) -> bool:
    """Return True unless *record_type* is on the blocklist.

    Only system/security records are blocked. Everything else is allowed —
    HITL confirmation is the primary safety layer.
    """
    return record_type not in _BLOCKED_RECORD_TYPES


def generate_confirmation_token(
    session_id: str,
    payload_json: str,
    event_type: str = "write_confirm",
) -> str:
    """Generate an HMAC-SHA256 token binding *payload_json* to *session_id*.

    The token is a 64-character hex digest.  Uses ``settings.JWT_SECRET_KEY``
    as the HMAC key so tokens are server-side secrets the browser cannot
    forge.

    The ``event_type`` parameter scopes the token to a specific confirmation
    namespace (e.g. ``"write_confirm"`` for PR #39 write-back confirmation,
    ``"plan_mode_choice"`` for Plan Mode option selection). Cross-event
    isolation is enforced by including the event type in the HMAC message
    bytes so a write_confirm token cannot be replayed as a plan_mode_choice
    (and vice versa).

    Backward compat: ``event_type="write_confirm"`` (the default) reproduces
    the pre-event-type message format byte-for-byte (``f"{session_id}:{payload_json}"``)
    so existing PR #39 tokens remain valid with zero changes to callers.
    """
    from app.core.config import settings  # lazy import — avoids circular dep

    if event_type == "write_confirm":
        # Backward-compatible bytes — preserve pre-event-type token format
        # so PR #39 tokens generated before this kwarg existed still verify.
        message = f"{session_id}:{payload_json}".encode()
    else:
        message = f"{event_type}:{session_id}:{payload_json}".encode()
    return hmac.new(
        settings.JWT_SECRET_KEY.encode(),
        message,
        hashlib.sha256,
    ).hexdigest()


def verify_confirmation_token(
    token: str,
    session_id: str,
    payload_json: str,
    event_type: str = "write_confirm",
) -> bool:
    """Return True if *token* is valid for the given session, payload, and event type.

    Uses ``hmac.compare_digest`` to prevent timing attacks. Default
    ``event_type="write_confirm"`` preserves PR #39 backward compat — tokens
    generated without an explicit event_type validate without one.
    """
    expected = generate_confirmation_token(session_id, payload_json, event_type=event_type)
    return hmac.compare_digest(expected, token)
