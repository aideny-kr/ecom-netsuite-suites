"""Tests for Task 7 — server-declared editable slots + server-side merge on approve.

Security contract under test: the client may submit values only for field
names the SERVER declared editable (``editable_slots``), and only values
inside a slot's ``allowed`` set when one exists. Everything else is
rejected. ``merge_slot_values`` is the single choke point that enforces
this before a merged payload is ever handed to ``execute_tool_call`` — see
``backend/app/services/chat/write_confirmation_service.py``.

The first block below (``merge_slot_values`` unit tests) is the failing
test set specified in the Task 7 brief, verbatim. The second block
(``TestOrchestratorApproveWithSlotValues``) exercises the *actual*
orchestrator approve branch — not just the helper in isolation — because
unit suites in this area have previously passed against broken
integration behaviour (see task-7-brief.md care points).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.chat import ChatMessage, ChatSession
from app.services.chat.mutation_guard import generate_confirmation_token
from app.services.chat.write_confirmation_service import (
    _build_payload_json,
    merge_slot_values,
    validate_and_extract_confirmation,
)

SESSION = "11111111-1111-1111-1111-111111111111"
TOOL = "ext__" + "a" * 32 + "__ns_createRecord"


def _pending(slots):
    tool_input = {"recordType": "customer", "data": '{"companyname": "test ai customer"}'}
    return {
        "type": "write_confirmation",
        "mutation_type": "create",
        "record_type": "customer",
        "tool_name": TOOL,
        "tool_input": tool_input,
        "editable_slots": slots,
        # editable_slots is inside the HMAC envelope (review round 2) — sign
        # with the SAME slots this card carries, or every test built on this
        # helper would mint a token that never validates.
        "confirmation_token": generate_confirmation_token(SESSION, _build_payload_json(TOOL, tool_input, slots)),
        "status": "pending",
    }


SLOTS = [
    {
        "name": "subsidiary",
        "label": "Primary Subsidiary",
        "type": "select",
        "allowed": [{"value": "1", "label": "Framework Inc"}, {"value": "2", "label": "Framework EU"}],
    }
]


def test_declared_slot_value_is_merged():
    ok, tool_name, merged, err = merge_slot_values(_pending(SLOTS), {"subsidiary": "2"}, SESSION)
    assert ok is True
    assert err == ""
    import json

    assert json.loads(merged["data"])["subsidiary"] == "2"
    assert json.loads(merged["data"])["companyname"] == "test ai customer"


def test_undeclared_field_is_rejected():
    ok, _, _, err = merge_slot_values(_pending(SLOTS), {"companyname": "evil"}, SESSION)
    assert ok is False
    assert "not editable" in err


def test_value_outside_allowlist_is_rejected():
    ok, _, _, err = merge_slot_values(_pending(SLOTS), {"subsidiary": "99"}, SESSION)
    assert ok is False
    assert "not an allowed value" in err


def test_merged_payload_gets_a_fresh_token_and_old_one_dies():
    pending = _pending(SLOTS)
    old_token = pending["confirmation_token"]
    ok, tool_name, merged, _ = merge_slot_values(pending, {"subsidiary": "2"}, SESSION)
    assert ok is True
    stale = {"confirmation_token": old_token, "tool_name": tool_name, "tool_input": merged}
    assert validate_and_extract_confirmation(stale, SESSION)[0] is False


def test_no_slots_and_no_values_is_a_passthrough():
    ok, _, merged, err = merge_slot_values(_pending([]), {}, SESSION)
    assert ok is True and err == ""


# ---------------------------------------------------------------------------
# Review round 2 (operator-approved) — editable_slots must be inside the
# HMAC envelope. It is an authorization surface (it decides which fields a
# client may write and with which values), not just display data. An
# attacker with a write primitive against `chat_messages.structured_output`
# — NOT the HMAC secret — could previously append a slot declaring any
# field editable with any allowed value and have it survive
# `merge_slot_values`'s allowlist check, because the token never covered
# `editable_slots`. `tool_input` was always protected the same way; the gap
# was specific to slots. The negative pair below proves the hole is closed;
# the positive pair proves the fix isn't so brittle it breaks legitimate
# approvals (a signature that never matches is as useless as one that
# always does) — see reviewer note: "you will not notice from the negative
# tests alone."
# ---------------------------------------------------------------------------


def test_appending_a_slot_after_minting_invalidates_the_token():
    pending = _pending(SLOTS)
    tampered = dict(pending)
    tampered["editable_slots"] = SLOTS + [
        {"name": "companyname", "label": "Company Name", "type": "text", "allowed": None}
    ]
    is_valid, _, _ = validate_and_extract_confirmation(tampered, SESSION)
    assert is_valid is False


def test_loosening_an_allowed_list_after_minting_invalidates_the_token():
    pending = _pending(SLOTS)
    tampered = dict(pending)
    tampered["editable_slots"] = [
        {**SLOTS[0], "allowed": SLOTS[0]["allowed"] + [{"value": "99", "label": "Rogue Subsidiary"}]}
    ]
    is_valid, _, _ = validate_and_extract_confirmation(tampered, SESSION)
    assert is_valid is False


def test_unmodified_card_still_validates():
    pending = _pending(SLOTS)
    is_valid, _, _ = validate_and_extract_confirmation(pending, SESSION)
    assert is_valid is True


def test_slot_order_does_not_affect_validation():
    """Canonical ordering: two logically identical slot sets that merely
    differ in list order must sign to the SAME token — sort_keys=True only
    orders keys within each slot dict, not the list itself, so the signer
    must sort the slot list too, or every legitimate approval whose slots
    happen to arrive in a different order would spuriously fail."""
    two_slots = [
        {
            "name": "subsidiary",
            "label": "Primary Subsidiary",
            "type": "select",
            "allowed": [{"value": "1", "label": "Framework Inc"}],
        },
        {
            "name": "department",
            "label": "Department",
            "type": "select",
            "allowed": [{"value": "10", "label": "Sales"}],
        },
    ]
    tool_input = {"recordType": "customer", "data": '{"companyname": "test ai customer"}'}
    token = generate_confirmation_token(SESSION, _build_payload_json(TOOL, tool_input, two_slots))
    reordered = {
        "tool_name": TOOL,
        "tool_input": tool_input,
        "confirmation_token": token,
        "editable_slots": list(reversed(two_slots)),
    }
    is_valid, _, _ = validate_and_extract_confirmation(reordered, SESSION)
    assert is_valid is True


# ---------------------------------------------------------------------------
# Review round 1, CRITICAL — the merge must not drop line items.
#
# Every fixture above uses a flat `{"companyname": ...}` payload with no
# lines, which is exactly why the original merge (rebuilding `data` from
# `.fields` alone) shipped with a bug that silently dropped every line item:
# `normalize_write_payload` splits `.lines` out of the record with no way
# back to the original sublist name (`line`/`item`/`expense`/...), so a
# human approving "2 lines" would see 0 lines written. The fix merges into
# `NormalizedPayload.record` (the raw, unsplit dict) and writes back under
# `NormalizedPayload.payload_key` (the key it actually came from) instead.
# ---------------------------------------------------------------------------

LINE_SLOTS = [
    {
        "name": "subsidiary",
        "label": "Primary Subsidiary",
        "type": "select",
        "allowed": [{"value": "1", "label": "Framework Inc"}, {"value": "2", "label": "Framework EU"}],
    }
]


def _pending_with_lines(slots, *, shape: str):
    """A transaction payload with BOTH a header field and line items, in
    either the `data`-as-JSON-string shape (live NetSuite MCP) or the
    `body`-as-dict shape."""
    record = {"entity": "123", "item": [{"item": "5", "quantity": 1}, {"item": "9", "quantity": 4}]}
    tool_input = {"recordType": "salesOrder", shape: json.dumps(record) if shape == "data" else record}
    return {
        "type": "write_confirmation",
        "mutation_type": "create",
        "record_type": "salesOrder",
        "tool_name": TOOL,
        "tool_input": tool_input,
        "editable_slots": slots,
        "confirmation_token": generate_confirmation_token(SESSION, _build_payload_json(TOOL, tool_input, slots)),
        "status": "pending",
    }


def test_merge_preserves_line_items_in_data_shape():
    ok, _, merged, err = merge_slot_values(_pending_with_lines(LINE_SLOTS, shape="data"), {"subsidiary": "2"}, SESSION)
    assert ok is True, err
    record = json.loads(merged["data"])
    assert record["entity"] == "123"
    assert record["subsidiary"] == "2"
    assert record["item"] == [{"item": "5", "quantity": 1}, {"item": "9", "quantity": 4}]


def test_merge_preserves_line_items_in_body_shape():
    ok, _, merged, err = merge_slot_values(_pending_with_lines(LINE_SLOTS, shape="body"), {"subsidiary": "2"}, SESSION)
    assert ok is True, err
    record = merged["body"]
    assert record["entity"] == "123"
    assert record["subsidiary"] == "2"
    assert record["item"] == [{"item": "5", "quantity": 1}, {"item": "9", "quantity": 4}]


def test_merge_writes_back_to_the_key_normalize_actually_read():
    """Important #6: `data` is present-but-null; `body` is what
    `normalize_write_payload` actually parses. Writing the merge into `data`
    (as the old `if "data" in merged_input` check did — key PRESENCE, not
    which key actually coerced) leaves two conflicting payloads and lets the
    MCP tool decide which one wins."""
    tool_input = {"recordType": "customer", "data": None, "body": {"companyname": "test ai customer"}}
    pending = {
        "type": "write_confirmation",
        "mutation_type": "create",
        "record_type": "customer",
        "tool_name": TOOL,
        "tool_input": tool_input,
        "editable_slots": SLOTS,
        "confirmation_token": generate_confirmation_token(SESSION, _build_payload_json(TOOL, tool_input, SLOTS)),
        "status": "pending",
    }
    ok, _, merged, err = merge_slot_values(pending, {"subsidiary": "2"}, SESSION)
    assert ok is True, err
    assert merged["data"] is None
    assert merged["body"]["subsidiary"] == "2"
    assert merged["body"]["companyname"] == "test ai customer"


# ---------------------------------------------------------------------------
# Review round 1, Important #5 — a non-dict `slot_values` must not crash
# the SSE stream. `write_confirm` is a bare `dict | None` with no inner
# schema, so `slot_values` can be any JSON value; a list or string passes
# the truthiness gate (`if _slot_values:`) and would previously reach
# `.items()`, raising AttributeError mid-stream.
# ---------------------------------------------------------------------------


def test_non_dict_slot_values_is_rejected_not_crashed():
    ok, _, _, err = merge_slot_values(_pending(SLOTS), ["subsidiary", "2"], SESSION)
    assert ok is False
    assert err


# ---------------------------------------------------------------------------
# Review round 1, Important #3 — `allowed: []` must fail closed (not
# degrade to unconstrained free text), and non-scalar values must be
# rejected even for a slot with no allowlist at all.
# ---------------------------------------------------------------------------

EMPTY_ALLOWED_SLOTS = [{"name": "subsidiary", "label": "Primary Subsidiary", "type": "select", "allowed": []}]
TEXT_SLOT_NO_ALLOWLIST = [{"name": "memo", "label": "Memo", "type": "text", "allowed": None}]


def test_empty_allowed_list_fails_closed():
    ok, _, _, err = merge_slot_values(_pending(EMPTY_ALLOWED_SLOTS), {"subsidiary": "anything"}, SESSION)
    assert ok is False
    assert "subsidiary" in err


def test_nonscalar_slot_value_is_rejected():
    ok, _, _, err = merge_slot_values(_pending(TEXT_SLOT_NO_ALLOWLIST), {"memo": {"nested": "obj"}}, SESSION)
    assert ok is False


def test_null_slot_value_is_rejected():
    ok, _, _, err = merge_slot_values(_pending(TEXT_SLOT_NO_ALLOWLIST), {"memo": None}, SESSION)
    assert ok is False


def test_list_slot_value_is_rejected():
    ok, _, _, err = merge_slot_values(_pending(TEXT_SLOT_NO_ALLOWLIST), {"memo": ["a", "b"]}, SESSION)
    assert ok is False


def test_scalar_slot_value_with_no_allowlist_is_accepted():
    """The other side of Important #3 — a legitimate free-text slot must
    still work; only non-scalars and empty allowlists fail closed."""
    ok, _, merged, err = merge_slot_values(_pending(TEXT_SLOT_NO_ALLOWLIST), {"memo": "please expedite"}, SESSION)
    assert ok is True, err
    assert json.loads(merged["data"])["memo"] == "please expedite"


# ---------------------------------------------------------------------------
# Orchestrator integration — the approve branch, not the helper in isolation.
#
# `_confirm_msg` must be a REAL (transient, never-flushed) ChatMessage
# instance rather than a MagicMock(spec=...): the approve branch calls
# `sqlalchemy.orm.attributes.flag_modified(_confirm_msg, "structured_output")`,
# which reads `_sa_instance_state` — an attribute SQLAlchemy's instrumentation
# adds at object construction, not one `spec=ChatMessage` (built from `dir()`)
# knows about. A MagicMock there raises AttributeError, so any test that
# mocked ChatMessage here would never actually reach the merge logic.
# ---------------------------------------------------------------------------


_TENANT_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()


class _FakeResult:
    def __init__(self, msg):
        self._msg = msg

    def scalar_one_or_none(self):
        return self._msg


def _make_confirm_message(structured_output: dict) -> ChatMessage:
    msg = ChatMessage(
        tenant_id=_TENANT_ID,
        session_id=uuid.UUID(SESSION),
        role="assistant",
        content="",
        structured_output=structured_output,
        created_at=datetime.now(timezone.utc),
    )
    msg.id = uuid.uuid4()
    return msg


def _make_db(confirm_msg: ChatMessage) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(return_value=_FakeResult(confirm_msg))
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    return db


def _make_session() -> MagicMock:
    session = MagicMock(spec=ChatSession)
    session.id = uuid.UUID(SESSION)
    return session


class TestOrchestratorApproveWithSlotValues:
    @pytest.mark.asyncio
    async def test_approve_merges_declared_slot_before_executing(self):
        from app.services.chat.orchestrator import run_chat_turn

        confirm_msg = _make_confirm_message(_pending(SLOTS))
        db = _make_db(confirm_msg)
        session = _make_session()

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))
        mock_log_event = AsyncMock(return_value=None)

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
        ):
            events = [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="approve",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={
                        "action": "approve",
                        "confirmation_id": str(confirm_msg.id),
                        "slot_values": {"subsidiary": "2"},
                    },
                )
            ]

        assert not [e for e in events if e.get("type") == "error"]
        mock_execute_tool_call.assert_awaited_once()
        sent_tool_input = mock_execute_tool_call.await_args.kwargs["tool_input"]
        merged_fields = json.loads(sent_tool_input["data"])
        assert merged_fields["subsidiary"] == "2"
        assert merged_fields["companyname"] == "test ai customer"

        message_events = [e for e in events if e.get("type") == "message"]
        assert message_events
        assert "Done" in message_events[-1]["message"]["content"]

    @pytest.mark.asyncio
    async def test_approve_rejects_undeclared_slot_before_executing(self):
        """A field the server never declared editable must never reach NetSuite.

        This is the case that matters most: a manipulated browser submitting
        a value for a field it was never offered a slot for.
        """
        from app.services.chat.orchestrator import run_chat_turn

        confirm_msg = _make_confirm_message(_pending(SLOTS))
        db = _make_db(confirm_msg)
        session = _make_session()

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))
        mock_log_event = AsyncMock(return_value=None)

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
        ):
            events = [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="approve",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={
                        "action": "approve",
                        "confirmation_id": str(confirm_msg.id),
                        "slot_values": {"companyname": "evil"},
                    },
                )
            ]

        mock_execute_tool_call.assert_not_awaited()
        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events
        assert "not editable" in error_events[0]["error"]

    @pytest.mark.asyncio
    async def test_approve_rejects_value_outside_allowlist_before_executing(self):
        from app.services.chat.orchestrator import run_chat_turn

        confirm_msg = _make_confirm_message(_pending(SLOTS))
        db = _make_db(confirm_msg)
        session = _make_session()

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))
        mock_log_event = AsyncMock(return_value=None)

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
        ):
            events = [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="approve",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={
                        "action": "approve",
                        "confirmation_id": str(confirm_msg.id),
                        "slot_values": {"subsidiary": "99"},
                    },
                )
            ]

        mock_execute_tool_call.assert_not_awaited()
        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events
        assert "not an allowed value" in error_events[0]["error"]

    @pytest.mark.asyncio
    async def test_approve_with_no_slot_values_still_validates_and_executes(self):
        """Existing (pre-Task-7) approve flow with no slot editing must keep working."""
        from app.services.chat.orchestrator import run_chat_turn

        confirm_msg = _make_confirm_message(_pending([]))
        db = _make_db(confirm_msg)
        session = _make_session()

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))
        mock_log_event = AsyncMock(return_value=None)

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
        ):
            events = [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="approve",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={
                        "action": "approve",
                        "confirmation_id": str(confirm_msg.id),
                    },
                )
            ]

        assert not [e for e in events if e.get("type") == "error"]
        mock_execute_tool_call.assert_awaited_once()
        sent_tool_input = mock_execute_tool_call.await_args.kwargs["tool_input"]
        assert json.loads(sent_tool_input["data"])["companyname"] == "test ai customer"

    @pytest.mark.asyncio
    async def test_approve_persists_merged_payload_and_mints_a_fresh_token(self):
        """Review round 1, Important #4: the token must be re-minted over the
        merged payload, and the persisted card must show what was actually
        written — not the agent's pre-merge payload. `_updated_so = dict(_so)`
        previously carried the pre-merge `tool_input` forward unchanged, so
        the stored card would permanently show the agent's original payload
        rather than what got sent to NetSuite (the audit log has the merged
        truth either way — this is specifically about the persisted card)."""
        from app.services.chat.orchestrator import run_chat_turn

        pending = _pending(SLOTS)
        original_token = pending["confirmation_token"]
        confirm_msg = _make_confirm_message(pending)
        db = _make_db(confirm_msg)
        session = _make_session()

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))
        mock_log_event = AsyncMock(return_value=None)

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
        ):
            events = [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="approve",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={
                        "action": "approve",
                        "confirmation_id": str(confirm_msg.id),
                        "slot_values": {"subsidiary": "2"},
                    },
                )
            ]

        assert not [e for e in events if e.get("type") == "error"]

        persisted = confirm_msg.structured_output
        # The card now shows the MERGED payload, not the agent's original.
        assert json.loads(persisted["tool_input"]["data"])["subsidiary"] == "2"
        assert json.loads(persisted["tool_input"]["data"])["companyname"] == "test ai customer"
        # A fresh token, different from the pre-merge one...
        assert persisted["confirmation_token"] != original_token
        # ...that actually validates against the merged payload it's now
        # stored alongside — the card and the write agree.
        is_valid, _, _ = validate_and_extract_confirmation(persisted, SESSION)
        assert is_valid is True

    @pytest.mark.asyncio
    async def test_approve_with_no_slot_values_does_not_re_mint_the_token(self):
        """The no-op passthrough path must not spuriously re-mint — the
        original token and tool_input are still correct, so touching either
        would be pure churn with no corresponding change to explain it."""
        from app.services.chat.orchestrator import run_chat_turn

        pending = _pending([])
        original_token = pending["confirmation_token"]
        confirm_msg = _make_confirm_message(pending)
        db = _make_db(confirm_msg)
        session = _make_session()

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))
        mock_log_event = AsyncMock(return_value=None)

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
        ):
            events = [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="approve",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={
                        "action": "approve",
                        "confirmation_id": str(confirm_msg.id),
                    },
                )
            ]

        assert not [e for e in events if e.get("type") == "error"]
        persisted = confirm_msg.structured_output
        assert persisted["confirmation_token"] == original_token
        assert persisted["tool_input"] == pending["tool_input"]
