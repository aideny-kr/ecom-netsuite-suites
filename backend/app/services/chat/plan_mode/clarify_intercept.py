"""Clarify tool-call interceptor (Component 3)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

# Source-of-truth provider → canonical-source map lives in source_resolver
# (codex P2 refactor — also consumed by short_circuit.filter_tools_for_chosen_source).
from app.services.chat.plan_mode.source_resolver import (
    canonicalize_connector_providers as _canonicalize_connector_providers,
)

__all__ = [
    "InterceptError",
    "InterceptResult",
    "intercept_clarify_call",
]


@dataclass
class InterceptResult:
    """Successful intercept — structured_output ready to persist + emit."""

    structured_output: dict
    sse_payload: dict


@dataclass
class InterceptError:
    """Schema/validation failure — agent retries within turn (tool_result is_error=True)."""

    error_message: str


async def intercept_clarify_call(
    *,
    tool_input: dict,
    session_id: str,
    active_connectors: list[str],
    db: AsyncSession,  # noqa: ARG001 — reserved for Task 3.5 (audit logging, etc.)
) -> InterceptResult | InterceptError:
    """Validate a clarify tool_use input and build the structured_output payload.

    Schema invariants enforced:
    - 2-3 options total (matches CLARIFY_TOOL_SCHEMA)
    - Each option's ``source`` must be in ``active_connectors`` (otherwise dropped)
    - After filtering, >=2 connected options remain
    - Exactly one option has ``is_default=True``
    - ``ambiguity_summary`` is non-empty

    On success: returns ``InterceptResult`` with HMAC-bound structured_output.
    On failure: returns ``InterceptError``; caller feeds ``error_message`` back to
    the agent as a tool_result (``is_error=True``) so it can retry within the turn.
    """
    options = tool_input.get("options") or []
    summary = (tool_input.get("ambiguity_summary") or "").strip()

    if not summary:
        return InterceptError(error_message="ambiguity_summary is required")

    if len(options) < 2 or len(options) > 3:
        return InterceptError(error_message=f"options must be 2-3, got {len(options)}")

    # Filter to connected sources. ``active_connectors`` arrives as raw
    # provider strings (e.g. ``netsuite_mcp``, ``shopify_mcp``); the clarify
    # schema enum uses bare names (``netsuite``, ``shopify``). Translate
    # before membership testing or every option drops in production.
    canonical_sources = _canonicalize_connector_providers(active_connectors)
    connected_options = [o for o in options if o.get("source") in canonical_sources]
    if len(connected_options) < 2:
        return InterceptError(
            error_message=("fewer than 2 connected sources in options; answer with the single connected source default")
        )

    # Option-id validation: each id must be in {A, B, C} and unique within the
    # set. Duplicates would render duplicate React keys client-side and the
    # resume endpoint cannot disambiguate (returns 400 invalid option).
    allowed_ids = {"A", "B", "C"}
    seen_ids: set[str] = set()
    for opt in connected_options:
        opt_id = opt.get("id")
        if opt_id not in allowed_ids:
            return InterceptError(error_message=f"option id must be one of A/B/C, got {opt_id!r}")
        if opt_id in seen_ids:
            return InterceptError(error_message=f"option ids must be unique, got duplicate {opt_id!r}")
        seen_ids.add(opt_id)

    # Default validation
    defaults = [o for o in connected_options if o.get("is_default") is True]
    if len(defaults) == 0:
        return InterceptError(error_message="exactly one option must have is_default=True (got 0)")
    if len(defaults) > 1:
        return InterceptError(error_message="exactly one option must have is_default=True (got >1)")

    default_id = defaults[0].get("id")
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

    payload_for_hmac = json.dumps(
        {"options": connected_options, "default_id": default_id},
        sort_keys=True,
    )
    from app.services.chat.mutation_guard import generate_confirmation_token

    token = generate_confirmation_token(session_id, payload_for_hmac, event_type="plan_mode_choice")

    structured_output = {
        "type": "clarification",
        "status": "pending",
        "options": connected_options,
        "default_id": default_id,
        "ambiguity_summary": summary,
        "confirmation_token": token,
        "expires_at": expires_at,
    }
    sse_payload = dict(structured_output)

    return InterceptResult(structured_output=structured_output, sse_payload=sse_payload)
