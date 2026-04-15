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


def generate_confirmation_token(session_id: str, payload_json: str) -> str:
    """Generate an HMAC-SHA256 token binding *payload_json* to *session_id*.

    The token is a 64-character hex digest.  Uses ``settings.JWT_SECRET_KEY``
    as the HMAC key so tokens are server-side secrets the browser cannot
    forge.
    """
    from app.core.config import settings  # lazy import — avoids circular dep

    message = f"{session_id}:{payload_json}".encode()
    return hmac.new(
        settings.JWT_SECRET_KEY.encode(),
        message,
        hashlib.sha256,
    ).hexdigest()


def verify_confirmation_token(token: str, session_id: str, payload_json: str) -> bool:
    """Return True if *token* is valid for the given session and payload.

    Uses ``hmac.compare_digest`` to prevent timing attacks.
    """
    expected = generate_confirmation_token(session_id, payload_json)
    return hmac.compare_digest(expected, token)
