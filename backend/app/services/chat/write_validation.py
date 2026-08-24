"""The single entry point for validating a NetSuite write payload.

LAW: every chat-loop write payload headed for ``execute_tool_call`` must pass
through ``validate_mutation()`` at the moment its final form is known — once
when the agent first proposes it (``base_agent``'s mutation intercept), and
again after a human's ``slot_values`` are merged into it (the orchestrator's
approve path, only when a merge actually changed the payload). There is
deliberately no third call site: reaching ``execute_tool_call`` from the chat
write loop without passing through this function requires visibly bypassing
a named function, not merely forgetting one of several inline blocks.

This module owns the ENTIRE sequence — normalize -> ``get_record_metadata``
-> ``check_posting_invariants`` -> ``validate_write`` — including the shape
decision for a delete payload (``{recordType, id}``, no ``data``/``body``
key), so no call site can run a partial or reordered version of it and no
call site can disagree with another about what a delete payload looks like.
"""

from __future__ import annotations

from typing import Any, Literal

from app.services.chat.posting_invariants import check_posting_invariants
from app.services.chat.record_metadata_service import get_record_metadata
from app.services.chat.write_payload import NormalizedPayload, normalize_write_payload
from app.services.chat.write_validator import ValidationResult, validate_write

__all__ = ["normalize_for_validation", "validate_mutation"]


def normalize_for_validation(mutation_type: str, tool_input: dict[str, Any]) -> NormalizedPayload:
    """Return the payload shape validation should check.

    A delete carries only ``{recordType, id}`` — no ``data``/``body`` key —
    which is a structurally expected, non-error shape that
    ``normalize_write_payload`` (whose entire contract is "parse the
    data/body payload or raise") was never built to accept. Route it to a
    trivial stand-in instead of forcing it through that contract.

    Every other mutation type keeps today's fail-closed behavior exactly:
    an unparseable create/update/upsert payload still raises
    ``PayloadParseError``, propagated to the caller unchanged.
    """
    if mutation_type == "delete":
        record_id = tool_input.get("id")
        return NormalizedPayload(
            fields={},
            lines=[],
            record_id=str(record_id) if record_id is not None else None,
            record={},
            payload_key="",
        )
    return normalize_write_payload(tool_input)


async def validate_mutation(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    mutation_type: Literal["create", "update", "delete", "upsert"],
    record_type: str,
    tenant_id: Any,
    actor_id: Any,
    correlation_id: str,
    db: Any,
    session_id: str,
) -> ValidationResult:
    """Validate a write payload against live record metadata and posting
    invariants, in one call.

    Raises ``PayloadParseError`` for an unparseable create/update/upsert
    payload — callers must handle it the same way a direct
    ``normalize_write_payload`` call has always required (deletes never
    raise, per ``normalize_for_validation``).
    """
    payload = normalize_for_validation(mutation_type, tool_input)

    meta = await get_record_metadata(
        record_type=record_type,
        mutation_tool_name=tool_name,
        tenant_id=tenant_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
        db=db,
        session_id=session_id,
    )
    invariants = await check_posting_invariants(
        payload=payload,
        record_type=record_type,
        mutation_tool_name=tool_name,
        tenant_id=tenant_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
        db=db,
        session_id=session_id,
    )
    return validate_write(
        payload=payload,
        metadata=meta,
        record_type=record_type,
        mutation_type=mutation_type,
        invariant_errors=invariants,
    )
