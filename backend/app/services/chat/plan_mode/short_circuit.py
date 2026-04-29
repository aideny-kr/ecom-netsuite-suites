"""plan_mode_choice short-circuit handler.

Architectural twin of write_confirm short-circuit at orchestrator.py:1148.
Same persistence pattern (atomic CAS on ChatMessage.structured_output),
same HMAC-token validation (mutation_guard with event_type='plan_mode_choice'),
same one-shot semantics (status: pending -> chosen | rejected).

UNLIKE write_confirm, the success path does NOT terminate the turn — it
returns a PlanModeChoiceResult with a system_directive + chosen_source,
and the caller falls through into the regular agent flow with those
variables set.
"""

from __future__ import annotations

import json
import logging
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatMessage
from app.models.chat_disclosure_event import ChatDisclosureEvent
from app.services.chat.mutation_guard import verify_confirmation_token

logger = logging.getLogger(__name__)


@dataclass
class PlanModeChoiceResult:
    chosen_option: dict
    chosen_source: str
    system_directive: str  # PRIOR CLARIFICATIONS block to inject into resume turn


@dataclass
class PlanModeChoiceError:
    status_code: int  # 400, 403, 404, 409
    error: str


async def handle_plan_mode_choice(
    *,
    plan_mode_choice: dict,
    session_id: str,
    tenant_id: _uuid.UUID,
    db: AsyncSession,
) -> PlanModeChoiceResult | PlanModeChoiceError:
    """Validate the user's clarification choice and transition the prior
    structured_output from 'pending' to 'chosen'. Persists a
    chat_disclosure_events row with event_type='clarification_chose'.

    Returns a PlanModeChoiceResult on success — caller injects
    ``system_directive`` into the resume turn's system prompt and uses
    ``chosen_source`` to filter the resume turn's tool inventory.
    """
    confirmation_id_raw = plan_mode_choice.get("confirmation_id")
    option_id = plan_mode_choice.get("option_id")
    action = plan_mode_choice.get("action")

    if action != "approve":
        return PlanModeChoiceError(
            status_code=400,
            error=f"plan_mode_choice action must be 'approve', got {action!r}",
        )

    if not confirmation_id_raw or option_id not in ("A", "B", "C"):
        return PlanModeChoiceError(status_code=400, error="invalid plan_mode_choice payload")

    try:
        confirmation_id = _uuid.UUID(str(confirmation_id_raw))
    except (ValueError, TypeError):
        return PlanModeChoiceError(status_code=400, error="confirmation_id must be a valid UUID")

    # Load the prior assistant message
    result = await db.execute(select(ChatMessage).where(ChatMessage.id == confirmation_id))
    msg = result.scalar_one_or_none()
    if msg is None:
        return PlanModeChoiceError(status_code=404, error="message_not_found")

    # Confirm session match (cross-session replay protection)
    if str(msg.session_id) != str(session_id):
        return PlanModeChoiceError(status_code=403, error="message does not belong to this session")

    so = msg.structured_output or {}
    if not isinstance(so, dict) or so.get("type") != "clarification":
        return PlanModeChoiceError(status_code=400, error="not_a_clarification_message")

    if so.get("status") != "pending":
        return PlanModeChoiceError(
            status_code=409,
            error=f"already_resolved (status={so.get('status')})",
        )

    # Verify HMAC token (event_type-bound to plan_mode_choice)
    payload_for_hmac = json.dumps(
        {"options": so.get("options", []), "default_id": so.get("default_id")},
        sort_keys=True,
    )
    if not verify_confirmation_token(
        so.get("confirmation_token", ""),
        session_id,
        payload_for_hmac,
        event_type="plan_mode_choice",
    ):
        return PlanModeChoiceError(status_code=403, error="invalid_or_expired_token")

    # Find chosen option
    chosen = next((o for o in so.get("options", []) if o.get("id") == option_id), None)
    if chosen is None:
        return PlanModeChoiceError(
            status_code=400,
            error=f"option_id={option_id} not in options",
        )

    # Atomic CAS: pending -> chosen
    now_iso = datetime.now(timezone.utc).isoformat()
    cas = await db.execute(
        update(ChatMessage)
        .where(
            ChatMessage.id == confirmation_id,
            ChatMessage.structured_output["status"].astext == "pending",
        )
        .values(
            structured_output={
                **so,
                "status": "chosen",
                "chosen_id": option_id,
                "chose_at": now_iso,
            }
        )
    )
    if cas.rowcount == 0:
        return PlanModeChoiceError(
            status_code=409,
            error="concurrent_resolve — another request already transitioned this clarification",
        )

    # Persist resolution event
    db.add(
        ChatDisclosureEvent(
            tenant_id=tenant_id,
            chat_session_id=msg.session_id,
            chat_message_id=msg.id,
            event_type="clarification_chose",
            payload={
                "chosen_id": option_id,
                "chosen_source": chosen.get("source"),
            },
        )
    )
    await db.commit()

    # Build server-authored system directive (Codex finding 7 — DO NOT inject
    # synthetic XML user messages; this is a server-side prompt addition
    # that the model trusts implicitly).
    directive = (
        "## PRIOR CLARIFICATIONS\n\n"
        "User has clarified for this question:\n"
        f"- Metric/source: {chosen.get('title', '')} ({chosen.get('rationale', '')})\n"
        f"- Source: {chosen.get('source', '')}\n"
        "- Use this exact interpretation; do not switch sources without asking again."
    )

    return PlanModeChoiceResult(
        chosen_option=chosen,
        chosen_source=chosen.get("source", ""),
        system_directive=directive,
    )


# Tool name prefixes per source. Cross-source tools (see below) are
# always included on resume turns regardless of chosen source.
_SOURCE_TOOL_PREFIXES: dict[str, tuple[str, ...]] = {
    "netsuite": ("netsuite_", "ext__"),  # ext__ is MCP NetSuite
    "bigquery": ("bigquery_",),
    "shopify": ("shopify_",),
    "stripe": ("stripe_",),
    "drive": ("drive_",),
}

# Tools that work across all sources — keep regardless of chosen source.
_CROSS_SOURCE_TOOLS: frozenset[str] = frozenset(
    {
        "pivot_query_result",
        "docs_create",
        "drive_read_doc",
        "clarify",
        "reference_previous_result",
    }
)


def filter_tools_for_chosen_source(tools: list[dict], chosen_source: str) -> list[dict]:
    """Return only tools matching ``chosen_source`` + cross-source tools.

    Used on the resume turn after the user picks a clarification option, so
    the agent literally cannot call the wrong source's tools — even if the
    LLM tries, the schema doesn't include them. Order is preserved.
    """
    keep_prefixes = _SOURCE_TOOL_PREFIXES.get(chosen_source, ())
    filtered: list[dict] = []
    for tool in tools:
        name = tool.get("name", "")
        if name in _CROSS_SOURCE_TOOLS:
            filtered.append(tool)
        elif keep_prefixes and any(name.startswith(p) for p in keep_prefixes):
            filtered.append(tool)
    return filtered
