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
from app.services.chat.write_payload import PayloadParseError, normalize_write_payload
from app.services.chat.write_validator import EditableSlot, ValidationResult

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
    proposed_lines: list[dict[str, Any]] = []
    current_record: dict[str, Any] | None = None
    tool_name: str
    tool_input: dict[str, Any]
    confirmation_token: str
    status: Literal["pending", "approved", "rejected", "failed"] = "pending"
    editable_slots: list[EditableSlot] = []
    unvalidated: bool = False
    # Missing *line*-level required fields have no slot to fill (line-level
    # editing needs nested UI + a merge path that writes back into the right
    # line — out of scope here; see ClickUp 86bbgznjr). When non-empty, the
    # card is terminal: it names these fields and renders no form — a
    # half-form the human could approve, that then fails at NetSuite anyway,
    # is worse than an honest stop.
    unfillable_line_fields: list[str] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_payload_json(
    tool_name: str,
    tool_input: dict[str, Any],
    editable_slots: list[dict[str, Any]],
) -> str:
    """Canonical JSON representation used for HMAC signing.

    ``editable_slots`` is inside the signed envelope alongside
    ``tool_name``/``tool_input``: it decides which fields a client may write
    on approve and with which values, so it is an authorization surface, not
    display data. Every mint site and every verify site must call this with
    the SAME slots the card actually carries — a slot appended, or an
    ``allowed`` list loosened, after minting must invalidate the token
    exactly like a tampered ``tool_input`` does.

    ``editable_slots`` must already be plain dicts (e.g. via
    ``EditableSlot.model_dump()`` on the mint side; a persisted
    ``ChatMessage.structured_output["editable_slots"]`` is already dicts on
    the verify side) — never raw pydantic model instances. ``default=str``
    would serialize a pydantic object to its repr string, which differs from
    the JSON serialization of the same data as a dict, so signing objects
    directly on one side and dicts on the other would mean no token ever
    verifies.

    ``sort_keys=True`` orders keys *within* each slot dict but not the slot
    *list* itself, so the slots are additionally sorted here — by
    ``(name, label)`` rather than ``name`` alone, since ``sorted()`` is
    stable and two slots sharing a ``name`` would otherwise sign differently
    depending on which arrived first. Two logically identical slot sets must
    sign to the same string regardless of the order they happen to arrive
    in.
    """
    sorted_slots = sorted(editable_slots, key=lambda slot: (slot.get("name", ""), slot.get("label", "")))
    return json.dumps(
        {"tool_name": tool_name, "tool_input": tool_input, "editable_slots": sorted_slots},
        sort_keys=True,
        default=str,
    )


def _is_valid_editable_slots_shape(value: Any) -> bool:
    """A stored ``editable_slots`` value must be a list of dicts.

    Not client-reachable — this field is only ever written by
    ``EditableSlot.model_dump()`` — but it sits on the unconditional
    token-check path (``validate_and_extract_confirmation``) and the merge
    path (``merge_slot_values``), both of which iterate it. A corrupted or
    unexpectedly-shaped stored value (``None``, a list of strings, ...) must
    fail closed — return ``False`` — rather than raise inside an SSE
    generator.
    """
    return isinstance(value, list) and all(isinstance(slot, dict) for slot in value)


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
    validation: "ValidationResult | None" = None,
) -> WriteConfirmationPayload | None:
    """Build a ``WriteConfirmationPayload`` for a pending write operation.

    Returns ``None`` in two distinct cases the caller must not conflate:

    1. *record_type* is not on the mutation allowlist (either explicitly
       blocked or simply unknown — deny by default).
    2. *record_type* is allowed, but the payload is missing or unparseable
       for a create/update/upsert (``normalize_write_payload`` raised
       ``PayloadParseError``, caught here so this function never raises).

    Callers that need to tell these apart — to show an accurate error
    message — should re-check ``is_record_type_allowed(record_type)``: if it
    is ``True`` and this function returned ``None``, the payload was the
    problem, not the record type.

    Deletes legitimately carry no field payload — only ``{recordType, id}``
    — so they are never subject to the parse-error case and always return a
    payload (with empty ``proposed_fields``/``proposed_lines``) once the
    record type is allowed.

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
    validation:
        Optional ``ValidationResult`` from ``validate_write`` (Task 6). Its
        ``editable_slots``, ``unvalidated``, and ``missing_line_required``
        flow onto the card so the human sees exactly what the server will
        accept back, and Task 9 can render — or refuse to render — a form.
    """
    if not is_record_type_allowed(record_type):
        return None

    # Delete operations legitimately have no field payload — only the record ID.
    # Create/update/upsert require a payload.
    if mutation_type == "delete":
        record_id: str | None = tool_input.get("id")
        if record_id is not None:
            record_id = str(record_id)
        # Compute the slots FIRST so the token is signed over the exact
        # slots the card carries — editable_slots is inside the HMAC
        # envelope now (an authorization surface, not just display data).
        editable_slots = list(validation.editable_slots) if validation else []
        payload_json = _build_payload_json(tool_name, tool_input, [s.model_dump() for s in editable_slots])
        confirmation_token = generate_confirmation_token(session_id, payload_json)
        return WriteConfirmationPayload(
            mutation_type=mutation_type,
            record_type=record_type,
            record_id=record_id,
            proposed_fields={},
            proposed_lines=[],
            current_record=current_record,
            tool_name=tool_name,
            tool_input=tool_input,
            confirmation_token=confirmation_token,
            editable_slots=editable_slots,
            unvalidated=bool(validation.unvalidated) if validation else False,
            unfillable_line_fields=list(validation.missing_line_required) if validation else [],
        )

    # For create/update/upsert, require a parseable payload. Fail closed:
    # a missing or malformed payload must never produce an empty-but-
    # approvable card, so an unparseable payload returns None here — same
    # as a blocked record type — rather than raising into the caller's
    # streaming loop. See the docstring for how callers distinguish the two.
    try:
        normalized = normalize_write_payload(tool_input)
    except PayloadParseError:
        return None

    editable_slots = list(validation.editable_slots) if validation else []
    payload_json = _build_payload_json(tool_name, tool_input, [s.model_dump() for s in editable_slots])
    confirmation_token = generate_confirmation_token(session_id, payload_json)

    return WriteConfirmationPayload(
        mutation_type=mutation_type,
        record_type=record_type,
        record_id=normalized.record_id,
        proposed_fields=normalized.fields,
        proposed_lines=normalized.lines,
        current_record=current_record,
        tool_name=tool_name,
        tool_input=tool_input,
        confirmation_token=confirmation_token,
        editable_slots=editable_slots,
        unvalidated=bool(validation.unvalidated) if validation else False,
        unfillable_line_fields=list(validation.missing_line_required) if validation else [],
    )


def build_recon_group_confirmation(
    tool_input: dict[str, Any],
    session_id: str,
) -> WriteConfirmationPayload:
    """Build a ``WriteConfirmationPayload`` for a pending ``recon.approve_group``
    bulk-approve (Phase 2 chat tool).

    Unlike ``build_confirmation_payload``, this bypasses the NetSuite
    ``is_record_type_allowed`` record-type allowlist entirely — a resolution
    group is not a NetSuite record type, so that gate would always deny it.
    Reuses ``generate_confirmation_token`` with the DEFAULT ``event_type``
    (``"write_confirm"``) so the orchestrator's existing
    ``validate_and_extract_confirmation`` approve path (chat runs API) works
    completely unchanged for this new tool.

    Parameters
    ----------
    tool_input:
        The raw ``recon_approve_group`` tool-use input from the LLM
        (``run_id``, ``group_key``, optional ``currency``/``notes``).
    session_id:
        The current chat session ID — bound into the HMAC token.
    """
    group_key = tool_input.get("group_key", "")
    currency = tool_input.get("currency")
    notes = tool_input.get("notes")

    proposed_fields: dict[str, Any] = {
        "group": group_key,
        "currency": currency,
        "notes": notes,
        # Item count is unknown at card-build time (no DB lookup here) —
        # omit rather than guess/fabricate a number the user would see.
    }

    # No slots on this card — pass an empty list explicitly so signing here
    # stays consistent with every other mint site now that editable_slots is
    # part of the signed envelope.
    payload_json = _build_payload_json("recon.approve_group", tool_input, [])
    confirmation_token = generate_confirmation_token(session_id, payload_json)

    return WriteConfirmationPayload(
        mutation_type="update",
        record_type="reconciliation group",
        record_id=group_key or None,
        proposed_fields=proposed_fields,
        current_record=None,
        tool_name="recon.approve_group",
        tool_input=tool_input,
        confirmation_token=confirmation_token,
    )


def validate_and_extract_confirmation(
    structured_output: dict[str, Any],
    session_id: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Validate a confirmation payload received from the frontend.

    Rebuilds the original ``payload_json`` from the ``tool_name``,
    ``tool_input``, and ``editable_slots`` fields in *structured_output* and
    verifies the HMAC token. ``editable_slots`` is inside the signed
    envelope: a slot appended, or an ``allowed`` list loosened, since minting
    must invalidate the token exactly like a tampered ``tool_input`` does —
    see ``_build_payload_json``. The stored slot list is already plain dicts
    (round-tripped through ``ChatMessage.structured_output``, a JSON
    column), so no ``.model_dump()`` is needed here.

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
    editable_slots: Any = structured_output.get("editable_slots", [])

    # A malformed stored value (None, a list of strings, ...) must fail
    # closed here rather than raise inside _build_payload_json's sort.
    if not _is_valid_editable_slots_shape(editable_slots):
        return False, tool_name, tool_input

    payload_json = _build_payload_json(tool_name, tool_input, editable_slots)
    is_valid = verify_confirmation_token(token, session_id, payload_json)

    return is_valid, tool_name, tool_input


# Values a human-filled form field could actually produce. A dict/list/None
# is never something a form emits for a single slot — rejecting them keeps
# a slot from becoming a channel for arbitrary structured data.
_SCALAR_TYPES = (str, int, float, bool)


def _is_scalar(value: Any) -> bool:
    return isinstance(value, _SCALAR_TYPES)


def mint_confirmation_token(
    tool_name: str,
    tool_input: dict[str, Any],
    editable_slots: list[dict[str, Any]],
    session_id: str,
) -> str:
    """Compute a fresh HMAC confirmation token for ``(tool_name, tool_input,
    editable_slots)``.

    Used after ``merge_slot_values`` merges human-supplied field values into
    a payload: the persisted card and the executed write must show the SAME
    payload, so the token has to be re-minted over the merged result rather
    than reusing the pre-merge token. ``editable_slots`` is unchanged by a
    merge (only the values filled into ``tool_input`` change) — callers pass
    the same slot list the card already carries.
    """
    return generate_confirmation_token(session_id, _build_payload_json(tool_name, tool_input, editable_slots))


def merge_slot_values(
    structured_output: dict[str, Any],
    slot_values: dict[str, Any],
    session_id: str,
) -> tuple[bool, str, dict[str, Any], str]:
    """Merge human-supplied values for server-declared editable slots.

    The client may only supply values for names the SERVER declared editable
    (``structured_output["editable_slots"]``), and only values inside each
    slot's ``allowed`` set when one exists. This is what stops a manipulated
    client authoring an ERP write: it can fill declared blanks with allowed
    values and nothing else — any other key, a non-scalar value, or a value
    outside an allowlist, is rejected before the payload ever reaches
    ``execute_tool_call``.

    ``structured_output`` is expected to be server-trusted (e.g. loaded from
    a persisted ``ChatMessage.structured_output``, never accepted verbatim
    from the client) — only ``slot_values`` is attacker-controlled input, and
    it can be any JSON value (not necessarily a dict), so its shape is
    checked before ``.items()`` is ever called on it.

    Returns ``(is_valid, tool_name, merged_tool_input, error)``. ``error`` is
    ``""`` on success.
    """
    tool_name: str = structured_output.get("tool_name", "")
    tool_input: dict[str, Any] = dict(structured_output.get("tool_input", {}))
    raw_slots: Any = structured_output.get("editable_slots", [])

    if not slot_values:
        # Pure passthrough — still validates the original token so a
        # no-op approve carries exactly the same guarantee it always has.
        # Doesn't touch raw_slots at all: validate_and_extract_confirmation
        # has its own malformed-shape guard.
        is_valid, name, original = validate_and_extract_confirmation(structured_output, session_id)
        return is_valid, name, original, "" if is_valid else "invalid token"

    if not isinstance(slot_values, dict):
        return False, tool_name, {}, "slot_values must be an object mapping field name to value."

    # A malformed stored value (None, a list of strings, ...) must fail
    # closed here rather than raise building `slots` below.
    if not _is_valid_editable_slots_shape(raw_slots):
        return False, tool_name, {}, "stored editable_slots is malformed."
    slots = {s["name"]: s for s in raw_slots}

    for key, value in slot_values.items():
        if key not in slots:
            return False, tool_name, {}, f"Field '{key}' is not editable."
        if not _is_scalar(value):
            return False, tool_name, {}, f"'{key}' must be a plain text, number, or boolean value."
        allowed = slots[key].get("allowed")
        if allowed is not None:
            # An explicitly empty allowlist means the server could not
            # constrain this slot (e.g. a lookup returned zero options) —
            # fail closed, the same as every other "can't constrain this"
            # case in this plan, rather than degrading to unconstrained
            # free text. `allowed is None` (no allowlist declared at all)
            # is the only case that permits an arbitrary scalar.
            if not allowed:
                return False, tool_name, {}, f"'{key}' has no allowed values and cannot be filled."
            permitted = {str(opt.get("value")) for opt in allowed}
            if str(value) not in permitted:
                return False, tool_name, {}, f"'{value}' is not an allowed value for '{key}'."

    try:
        normalized = normalize_write_payload(tool_input)
    except PayloadParseError as exc:
        return False, tool_name, {}, f"stored payload unparseable: {exc}"

    # Merge into the RAW record (fields + line-sublists together, exactly as
    # normalize_write_payload read it) and write back under the SAME key it
    # was read from. Merging only `.fields` and reassembling `.lines` under
    # a guessed key would silently drop every line item — `.lines` is a
    # flattened list with no way back to its original sublist name
    # (`line`/`item`/`expense`/...). Branching write-back on key PRESENCE
    # (`"data" in tool_input`) rather than which key actually coerced also
    # produced two conflicting payloads for a present-but-null `data` next
    # to a populated `body`; writing back to `normalized.payload_key`
    # resolves both by construction.
    merged_record = {**normalized.record, **slot_values}
    merged_input = dict(tool_input)
    original_value = tool_input.get(normalized.payload_key)
    if isinstance(original_value, str):
        merged_input[normalized.payload_key] = json.dumps(merged_record)
    else:
        merged_input[normalized.payload_key] = merged_record

    return True, tool_name, merged_input, ""
