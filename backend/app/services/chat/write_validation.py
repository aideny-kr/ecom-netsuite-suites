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
from app.services.chat.required_field_registry import apply_curated_requirements
from app.services.chat.write_payload import NormalizedPayload, normalize_write_payload
from app.services.chat.write_validator import ValidationResult, validate_write

__all__ = ["normalize_for_validation", "resolve_curated_metadata", "validate_mutation"]


async def _curated_metadata(
    *,
    payload: NormalizedPayload,
    record_type: str,
    tool_name: str,
    tenant_id: Any,
    actor_id: Any,
    correlation_id: str,
    db: Any,
    session_id: str,
):
    """Fetch live metadata and overlay the curated registry. THE one way any
    caller obtains metadata for a write.

    Two callers need this and they must not diverge: `validate_mutation`
    (which derives `missing_required` from it) and the `ask_user` slot
    resolution in ``agents/base_agent.py`` (which checks a hinted field name
    against it). A T2 gate round found them split — the ask_user path used raw
    `get_record_metadata` while validation used the curated overlay — so a
    field could be reported missing by one and "not a recognized field" by the
    other, bouncing to the repair loop the exact question that was meant to
    reach a human. The overlay is now unskippable by construction rather than
    by both call sites remembering to apply it.
    """
    meta = await get_record_metadata(
        record_type=record_type,
        mutation_tool_name=tool_name,
        tenant_id=tenant_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
        db=db,
        session_id=session_id,
    )
    return apply_curated_requirements(meta, record_type=record_type, fields=payload.fields)


async def resolve_curated_metadata(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    mutation_type: str,
    record_type: str,
    tenant_id: Any,
    actor_id: Any,
    correlation_id: str,
    db: Any,
    session_id: str,
):
    """Curated metadata for a write, addressed by its tool input.

    Same result `validate_mutation` validates against, for callers that hold
    the tool call rather than a parsed payload. The underlying fetch is served
    from `get_record_metadata`'s 1h (connector_id, record_type) cache, so
    calling this after `validate_mutation` in the same turn is a cache hit,
    not a second MCP round trip.
    """
    payload = normalize_for_validation(mutation_type, tool_input)
    return await _curated_metadata(
        payload=payload,
        record_type=record_type,
        tool_name=tool_name,
        tenant_id=tenant_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
        db=db,
        session_id=session_id,
    )


def normalize_for_validation(mutation_type: str, tool_input: dict[str, Any]) -> NormalizedPayload:
    """Return the payload shape validation should check.

    A delete carries only ``{recordType, id}`` — no ``data``/``body`` key —
    which is a structurally expected, non-error shape that
    ``normalize_write_payload`` (whose entire contract is "parse the
    data/body payload or raise") was never built to accept. Route it to a
    trivial stand-in instead of forcing it through that contract.

    What this achieves: a delete now reaches ``validate_mutation()`` at all
    — before this, an unparseable delete raised ``PayloadParseError`` before
    ``get_record_metadata``/``check_posting_invariants`` ever ran, so
    ``self._last_validation`` stayed ``None`` and the confirmation card's
    delete branch never received real data (it was dead code).

    What this does NOT achieve — read before assuming a delete is protected:
    the stand-in has ``fields={}`` and ``lines=[]``, which leaves BOTH
    posting invariants structurally incapable of firing for a delete.
    ``_check_period_open`` (``posting_invariants.py``) reads
    ``payload.fields.get("trandate")``; it is always absent here, so the
    function returns ``[]`` before it ever attempts the period query.
    ``_check_balanced`` sums ``payload.lines``; it is always ``[]`` here, so
    debits and credits are both zero and it reports BALANCED. Concretely: a
    delete of a journal entry in a closed period is NOT caught today.
    Resolving the period from the record being deleted (e.g. via
    ``ns_getRecord`` before the delete) is real work needing live-connector
    response-shape verification — tracked as ClickUp 86bbk2580, deliberately
    out of scope here.

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

    # The live NetSuite metadata shape carries field NAMES and nothing about
    # requiredness (see required_field_registry's docstring for the three
    # independent confirmations), so raw `requirements_known` is False and
    # `validate_write` would return early with an empty `missing_required` —
    # a card with nothing to ask for. The curated overlay happens here rather
    # than inside `get_record_metadata` because this is the layer holding the
    # payload the conditional rules read (`isperson` decides companyname vs
    # lastname), and `get_record_metadata` caches, so a payload-dependent
    # overlay applied there would leak one write's conditions into every
    # later one.
    meta = await _curated_metadata(
        payload=payload,
        record_type=record_type,
        tool_name=tool_name,
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
