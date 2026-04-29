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
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatMessage
from app.models.chat_disclosure_event import ChatDisclosureEvent
from app.services.chat.mutation_guard import verify_confirmation_token
from app.services.chat.plan_mode.source_resolver import PROVIDER_TO_CANONICAL_SOURCE
from app.services.chat.tools import parse_external_tool_name

logger = logging.getLogger(__name__)


@dataclass
class PlanModeChoiceResult:
    chosen_option: dict
    chosen_source: str
    system_directive: str  # PRIOR CLARIFICATIONS block to inject into resume turn
    # The id of the underlying ChatMessage row that was transitioned from
    # ``status='pending'`` to ``status='chosen'``. Exposed so the caller can
    # call ``revert_clarification_to_pending`` if a downstream FATAL step
    # (e.g. audit emission) fails — making the choice + audit effectively
    # atomic from the user's perspective. Optional for backward-compat.
    chat_message_id: _uuid.UUID | None = None


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

    # Enforce expires_at — fail-closed.
    #
    # The mint path (clarify_intercept.py) stamps a 5-minute expiry. The
    # HMAC token contains no timestamp, so without this check a stale
    # pending card from hours/days ago could be replayed by anyone who can
    # hit the endpoint. Treat missing or unparseable values as expired so a
    # malformed structured_output cannot bypass the gate. (codex P2)
    expires_raw = so.get("expires_at")
    expired = False
    if not expires_raw:
        logger.warning(
            "[PLAN_MODE] clarification %s missing expires_at — treating as expired",
            confirmation_id,
        )
        expired = True
    else:
        try:
            expires_dt = datetime.fromisoformat(str(expires_raw))
            # Defensive: assume UTC if the stamp is naive (mint path always
            # writes tz-aware ISO-8601, but be robust to upstream changes).
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= expires_dt:
                expired = True
        except (ValueError, TypeError):
            logger.warning(
                "[PLAN_MODE] clarification %s has unparseable expires_at=%r — treating as expired",
                confirmation_id,
                expires_raw,
            )
            expired = True
    if expired:
        return PlanModeChoiceError(status_code=410, error="expired")

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
        chat_message_id=msg.id,
    )


async def revert_clarification_to_pending(
    *,
    message_id: _uuid.UUID,
    tenant_id: _uuid.UUID,  # noqa: ARG001 — accepted for parity with sibling helpers
    db: AsyncSession,
) -> bool:
    """Atomically flip ``structured_output.status`` from ``'chosen'`` back
    to ``'pending'`` for ``message_id``.

    Used by the orchestrator when a FATAL step that follows
    ``handle_plan_mode_choice`` (e.g. ``log_event`` for ``plan_mode.chose``)
    raises — without this revert the row would be stuck at ``'chosen'``
    forever and the user would see HTTP 409 on every retry, with the card
    consumed but no answer ever produced (codex round 6 P2 Bug 1).

    Atomic: the CAS targets ``status='chosen'`` so a concurrent
    ``supersede_pending_clarifications`` (which transitions
    ``pending → superseded``) can never be silently undone — supersede
    can only see ``status='pending'`` rows in the first place, so the
    races don't overlap.

    Returns ``True`` if a row was reverted, ``False`` if no-op (e.g. the
    row had already been transitioned to a different state, or the id
    doesn't exist).
    """
    cas = await db.execute(
        update(ChatMessage)
        .where(
            ChatMessage.id == message_id,
            ChatMessage.structured_output["status"].astext == "chosen",
        )
        .values(
            # Surgical jsonb_set so we don't need to round-trip the row
            # back to Python just to flip a single key. Path is `{status}`.
            structured_output=func.jsonb_set(
                ChatMessage.structured_output,
                "{status}",
                '"pending"',
                False,
            )
        )
    )
    if cas.rowcount > 0:
        await db.commit()
        logger.warning(
            "[PLAN_MODE] reverted clarification %s back to pending after downstream failure",
            message_id,
        )
        return True
    return False


# Tool name prefixes per source. Cross-source tools (see below) are
# always included on resume turns regardless of chosen source.
#
# NOTE: ``ext__`` is intentionally NOT in any prefix list. ``ext__<uuid>__*``
# is the format for EVERY external MCP connector (NetSuite, Shopify, Stripe,
# BigQuery), so a prefix-only match would either keep them all (when chosen
# is the connector that happens to be aliased) or drop them all. The filter
# resolves each ``ext__`` tool's UUID to its connector provider via
# ``parse_external_tool_name`` + ``active_connectors`` (codex P2).
_SOURCE_TOOL_PREFIXES: dict[str, tuple[str, ...]] = {
    "netsuite": ("netsuite_",),
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


def _build_connector_uuid_to_canonical_source(
    active_connectors: list[Any] | None,
) -> dict[_uuid.UUID, str]:
    """Map each connector's UUID to its canonical clarify-source name.

    Connector objects are duck-typed: anything with ``.id`` (UUID) and
    ``.provider`` (string in ``PROVIDER_TO_CANONICAL_SOURCE``) qualifies.
    Connectors whose provider isn't in the canonical map are skipped — their
    ext__ tools won't survive any source filter (fail-closed).
    """
    if not active_connectors:
        return {}
    out: dict[_uuid.UUID, str] = {}
    for conn in active_connectors:
        conn_id = getattr(conn, "id", None)
        provider = getattr(conn, "provider", None)
        if conn_id is None or provider is None:
            continue
        canonical = PROVIDER_TO_CANONICAL_SOURCE.get(provider)
        if canonical is None:
            continue
        # ``conn.id`` is sometimes already a UUID, sometimes a hex string.
        try:
            uuid_key = conn_id if isinstance(conn_id, _uuid.UUID) else _uuid.UUID(str(conn_id))
        except (ValueError, TypeError):
            continue
        out[uuid_key] = canonical
    return out


def filter_tools_for_chosen_source(
    tools: list[dict],
    chosen_source: str,
    active_connectors: list[Any] | None = None,
) -> list[dict]:
    """Return only tools matching ``chosen_source`` + cross-source tools.

    Used on the resume turn after the user picks a clarification option, so
    the agent literally cannot call the wrong source's tools — even if the
    LLM tries, the schema doesn't include them. Order is preserved.

    For ``ext__<uuid>__*`` (external MCP) tools, the connector's provider
    is resolved from ``active_connectors`` and translated to a canonical
    source via ``PROVIDER_TO_CANONICAL_SOURCE``. The tool survives only if
    that canonical source matches ``chosen_source``. When
    ``active_connectors`` is ``None`` or empty, ext__ tools are dropped
    fail-closed (we cannot tell which provider they belong to).
    """
    keep_prefixes = _SOURCE_TOOL_PREFIXES.get(chosen_source, ())
    uuid_to_source = _build_connector_uuid_to_canonical_source(active_connectors)

    filtered: list[dict] = []
    for tool in tools:
        name = tool.get("name", "")
        if name in _CROSS_SOURCE_TOOLS:
            filtered.append(tool)
            continue
        # External MCP tool — resolve via connector UUID, not by prefix.
        parsed = parse_external_tool_name(name)
        if parsed is not None:
            connector_uuid, _raw = parsed
            ext_source = uuid_to_source.get(connector_uuid)
            if ext_source is not None and ext_source == chosen_source:
                filtered.append(tool)
            continue
        # Local tool — fall back to prefix match against chosen_source.
        if keep_prefixes and any(name.startswith(p) for p in keep_prefixes):
            filtered.append(tool)
    return filtered


async def supersede_pending_clarifications(
    *,
    session_id: str | _uuid.UUID,
    tenant_id: _uuid.UUID,
    db: AsyncSession,
) -> list[_uuid.UUID]:
    """Mark any pending clarification on this session as 'superseded'.

    Called at the top of every new turn when ``plan_mode_choice`` is NOT set
    (user typed instead of clicking the card). Atomic per-row CAS via
    ``UPDATE ... WHERE structured_output->>'status' = 'pending'``. Returns
    the list of ``ChatMessage.id`` values that were transitioned (empty list
    in the common case).

    Returning the IDs (not just a count) lets the caller emit a
    ``chat.plan_mode.superseded`` audit event per row — required for
    CFO-grade investigation trails (Task 6.4). Existing telemetry via
    ``chat_disclosure_events`` is unchanged.
    """
    # Normalise session_id to UUID for the query
    session_uuid = session_id if isinstance(session_id, _uuid.UUID) else _uuid.UUID(str(session_id))

    # Find pending clarifications first (we need their IDs for the telemetry event)
    pending_result = await db.execute(
        select(ChatMessage).where(
            ChatMessage.session_id == session_uuid,
            ChatMessage.structured_output["type"].astext == "clarification",
            ChatMessage.structured_output["status"].astext == "pending",
        )
    )
    pending_msgs = pending_result.scalars().all()
    if not pending_msgs:
        return []

    now_iso = datetime.now(timezone.utc).isoformat()
    transitioned_ids: list[_uuid.UUID] = []
    for msg in pending_msgs:
        so = msg.structured_output or {}
        # Atomic per-row CAS — guards against the resume turn race
        cas = await db.execute(
            update(ChatMessage)
            .where(
                ChatMessage.id == msg.id,
                ChatMessage.structured_output["status"].astext == "pending",
            )
            .values(
                structured_output={
                    **so,
                    "status": "superseded",
                    "chose_at": now_iso,
                }
            )
        )
        if cas.rowcount > 0:
            transitioned_ids.append(msg.id)
            db.add(
                ChatDisclosureEvent(
                    tenant_id=tenant_id,
                    chat_session_id=msg.session_id,
                    chat_message_id=msg.id,
                    event_type="clarification_superseded",
                    payload={
                        "ambiguity_summary": so.get("ambiguity_summary", ""),
                    },
                )
            )

    if transitioned_ids:
        await db.commit()
        logger.info(
            "[PLAN_MODE] superseded %d pending clarification(s) on session %s",
            len(transitioned_ids),
            session_id,
        )

    return transitioned_ids
