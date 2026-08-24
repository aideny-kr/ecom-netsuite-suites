"""Tests for write_validation.py — the single entry point that owns the
normalize -> get_record_metadata -> check_posting_invariants -> validate_write
sequence for every chat-loop write payload headed for execute_tool_call.

Findings B and C (T2 gate wave): B — the merged, slot-filled payload was
never re-validated at approve time; C — deletes were silently exempt from
the entire validation pipeline (they raised PayloadParseError out of
normalize_write_payload, a function whose contract they were never meant to
meet). Both are fixed by routing every call site through ``validate_mutation``
rather than patching each call site independently.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.write_payload import NormalizedPayload, PayloadParseError
from app.services.chat.write_validation import normalize_for_validation, validate_mutation
from app.services.chat.write_validator import ValidationResult

_HEX_32 = "a1b2c3d4e5f67890a1b2c3d4e5f67890"


def _ext(tool_name: str) -> str:
    return f"ext__{_HEX_32}__{tool_name}"


# ---------------------------------------------------------------------------
# BC1 — normalize_for_validation
# ---------------------------------------------------------------------------


class TestNormalizeForValidation:
    def test_delete_never_raises_and_carries_record_id(self):
        payload = normalize_for_validation("delete", {"recordType": "journalEntry", "id": "123"})
        assert payload.record_id == "123"
        assert payload.fields == {}
        assert payload.lines == []

    def test_delete_with_no_id_leaves_record_id_none(self):
        payload = normalize_for_validation("delete", {"recordType": "journalEntry"})
        assert payload.record_id is None

    def test_create_without_data_or_body_still_raises(self):
        """Non-delete mutation types keep today's fail-closed contract —
        normalize_for_validation must not swallow a genuinely unparseable
        create/update/upsert payload."""
        with pytest.raises(PayloadParseError):
            normalize_for_validation("create", {"recordType": "customer"})

    def test_create_with_data_parses_normally(self):
        payload = normalize_for_validation("create", {"recordType": "customer", "data": '{"companyname": "Acme"}'})
        assert payload.fields == {"companyname": "Acme"}

    def test_update_without_payload_still_raises(self):
        with pytest.raises(PayloadParseError):
            normalize_for_validation("update", {"recordType": "customer", "id": "1"})

    def test_upsert_without_payload_still_raises(self):
        with pytest.raises(PayloadParseError):
            normalize_for_validation("upsert", {"recordType": "customer"})


# ---------------------------------------------------------------------------
# C3 — characterization: delete mutation_type never produces missing_required
# / missing_line_required, regardless of metadata. Pins the behavior the
# delete fix depends on.
# ---------------------------------------------------------------------------


class TestValidateWriteDeleteCharacterization:
    def test_delete_ignores_required_fields_even_with_metadata(self):
        from app.services.chat.record_metadata_service import FieldSpec, RecordMetadata
        from app.services.chat.write_validator import validate_write

        metadata = RecordMetadata(
            record_type="journalEntry",
            fields=[FieldSpec(name="subsidiary", label="Subsidiary", required=True)],
        )
        result = validate_write(
            payload=NormalizedPayload(fields={}, lines=[]),
            metadata=metadata,
            record_type="journalEntry",
            mutation_type="delete",
            invariant_errors=[],
        )
        assert result.missing_required == []
        assert result.missing_line_required == []
        assert result.ok is True

    def test_delete_ok_is_driven_purely_by_invariant_errors(self):
        from app.services.chat.write_validator import validate_write

        result = validate_write(
            payload=NormalizedPayload(fields={}, lines=[]),
            metadata=None,
            record_type="journalEntry",
            mutation_type="delete",
            invariant_errors=["Accounting period 'Jul 2026' is closed — posting is not permitted."],
        )
        assert result.ok is False
        assert result.missing_required == []
        assert result.invariant_errors != []


# ---------------------------------------------------------------------------
# validate_mutation — wiring sanity (unit-level, real functions patched at
# their write_validation-module binding so a caller can spy/override them
# regardless of which entry point invoked validate_mutation).
# ---------------------------------------------------------------------------


class TestValidateMutationWiring:
    @pytest.mark.asyncio
    async def test_delete_flows_through_metadata_and_invariants(self):
        metadata_spy = AsyncMock(return_value=None)
        invariants_spy = AsyncMock(return_value=[])

        with (
            patch("app.services.chat.write_validation.get_record_metadata", metadata_spy),
            patch("app.services.chat.write_validation.check_posting_invariants", invariants_spy),
        ):
            result = await validate_mutation(
                tool_name=_ext("ns_deleteRecord"),
                tool_input={"recordType": "journalEntry", "id": "123"},
                mutation_type="delete",
                record_type="journalEntry",
                tenant_id=uuid.uuid4(),
                actor_id=uuid.uuid4(),
                correlation_id="c",
                db=None,
                session_id="s",
            )

        metadata_spy.assert_awaited_once()
        invariants_spy.assert_awaited_once()
        assert isinstance(result, ValidationResult)
        assert result.unvalidated is True
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_create_with_unparseable_payload_raises(self):
        with pytest.raises(PayloadParseError):
            await validate_mutation(
                tool_name=_ext("ns_createRecord"),
                tool_input={"recordType": "customer"},
                mutation_type="create",
                record_type="customer",
                tenant_id=uuid.uuid4(),
                actor_id=uuid.uuid4(),
                correlation_id="c",
                db=None,
                session_id="s",
            )

    @pytest.mark.asyncio
    async def test_invariant_violation_surfaces_on_the_result(self):
        invariants_spy = AsyncMock(return_value=["Accounting period 'Jul 2026' is closed — posting is not permitted."])
        with (
            patch("app.services.chat.write_validation.get_record_metadata", AsyncMock(return_value=None)),
            patch("app.services.chat.write_validation.check_posting_invariants", invariants_spy),
        ):
            result = await validate_mutation(
                tool_name=_ext("ns_deleteRecord"),
                tool_input={"recordType": "journalEntry", "id": "123"},
                mutation_type="delete",
                record_type="journalEntry",
                tenant_id=uuid.uuid4(),
                actor_id=uuid.uuid4(),
                correlation_id="c",
                db=None,
                session_id="s",
            )
        assert result.ok is False
        assert result.invariant_errors != []


# ---------------------------------------------------------------------------
# BC2 — the base_agent mutation intercept must route DELETE payloads through
# validate_mutation too, not just create/update/upsert. Drives the REAL
# run_streaming loop (same harness as test_write_repair_loop.py) so a wiring
# regression is caught here, not just inferred from a unit test on
# normalize_for_validation in isolation.
# ---------------------------------------------------------------------------


def _make_agent():
    from app.services.chat.agents.base_agent import BaseSpecialistAgent

    class _DeleteInterceptTestAgent(BaseSpecialistAgent):
        agent_name = "test_delete_intercept"
        max_steps = 3

        @property
        def system_prompt(self):
            return "test prompt"

        @property
        def tool_definitions(self):
            return [
                {
                    "name": _ext("ns_deleteRecord"),
                    "description": "delete a record",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ]

    agent = _DeleteInterceptTestAgent.__new__(_DeleteInterceptTestAgent)
    agent.tenant_id = uuid.uuid4()
    agent.user_id = uuid.uuid4()
    agent.correlation_id = "test-corr"
    return agent


async def _drive_delete_turn(
    tool_name: str,
    tool_input: dict,
    metadata_spy,
    invariants_spy,
    repair_attempts: int = 1,
    session_id: str | None = None,
):
    from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

    agent = _make_agent()

    tool_call_responses = [
        LLMResponse(
            text_blocks=[],
            tool_use_blocks=[ToolUseBlock(id=f"t{i}", name=tool_name, input=tool_input)],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        for i in range(repair_attempts)
    ]
    final_response = LLMResponse(
        text_blocks=["done"], tool_use_blocks=[], usage=TokenUsage(input_tokens=10, output_tokens=10)
    )
    # The SAME (tool_name, tool_input) repeated `repair_attempts` times has an
    # identical ValidationResult fingerprint each round — WriteRepairState
    # exits "stall" on the second identical failure rather than granting a
    # third attempt, at which point the loop falls through to build the
    # confirmation card despite the unresolved violation (same pattern as
    # test_write_repair_loop.py's stall-exhaustion test).
    responses = iter(tool_call_responses + [final_response])

    async def _fake_stream_message(**kwargs):
        yield "response", next(responses)

    mock_adapter = MagicMock()
    mock_adapter.stream_message = _fake_stream_message
    mock_adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})
    mock_adapter.build_tool_result_message = MagicMock(
        return_value={"role": "user", "content": [{"type": "tool_result"}]}
    )

    events = []
    with (
        patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
        patch("app.services.chat.write_validation.get_record_metadata", metadata_spy),
        patch("app.services.chat.write_validation.check_posting_invariants", invariants_spy),
        patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new_callable=AsyncMock),
        patch(
            "app.services.chat.agents.base_agent.extract_structured_confidence",
            new_callable=AsyncMock,
            return_value=MagicMock(score=4, source="mock"),
        ),
    ):
        async for event_type, payload in agent.run_streaming(
            task="Delete this journal entry.",
            context={},
            db=AsyncMock(),
            adapter=mock_adapter,
            model="test-model",
            session_id=session_id,
        ):
            events.append((event_type, payload))

    return agent, events


class TestDeletePayloadFlowsThroughValidation:
    @pytest.mark.asyncio
    async def test_delete_invokes_metadata_and_invariants_and_stashes_validation(self):
        """Finding C's proof: before the fix, a delete-shaped tool_input made
        normalize_write_payload raise PayloadParseError, so `if normalized is
        not None:` was skipped entirely — get_record_metadata and
        check_posting_invariants were NEVER invoked for a delete, and
        self._last_validation stayed None."""
        metadata_spy = AsyncMock(return_value=None)
        invariants_spy = AsyncMock(return_value=[])

        agent, events = await _drive_delete_turn(
            _ext("ns_deleteRecord"),
            {"recordType": "journalEntry", "id": "12345"},
            metadata_spy,
            invariants_spy,
        )

        metadata_spy.assert_awaited_once()
        invariants_spy.assert_awaited_once()
        assert agent._last_validation is not None

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert len(confirmations) == 1

    @pytest.mark.asyncio
    async def test_delete_with_invariant_violation_carries_it_on_the_card_and_is_blocked_on_approve(self):
        """C4 — end to end: a delete run through the intercept with
        check_posting_invariants mocked to return a closed-period violation
        must produce a confirmation card whose invariant_errors is non-empty,
        and the orchestrator's existing (7d04b6fe) approve gate must refuse
        it — proving Finding C's fix plus the already-shipped gate close the
        delete loop with ZERO changes to orchestrator.py."""
        metadata_spy = AsyncMock(return_value=None)
        invariants_spy = AsyncMock(return_value=["Accounting period 'Jul 2026' is closed — posting is not permitted."])
        session_id = str(uuid.uuid4())

        # The repair loop treats an invariant violation exactly like any
        # other validation failure — it gives the model a chance to retry
        # first. Drive the SAME delete twice so the second attempt "stall"s
        # (identical fingerprint) and the card is shown despite the
        # unresolved violation, per WriteRepairState's contract.
        _agent, events = await _drive_delete_turn(
            _ext("ns_deleteRecord"),
            {"recordType": "journalEntry", "id": "12345"},
            metadata_spy,
            invariants_spy,
            repair_attempts=2,
            session_id=session_id,
        )

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert len(confirmations) == 1
        card = confirmations[0]
        assert card["invariant_errors"] != []

        # Feed that exact card into the orchestrator's approve path and prove
        # the existing terminal gate (7d04b6fe) refuses it.
        from app.models.chat import ChatMessage, ChatSession
        from app.services.chat.orchestrator import run_chat_turn

        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        session_uuid = uuid.UUID(session_id)

        confirm_msg = ChatMessage(
            tenant_id=tenant_id,
            session_id=session_uuid,
            role="assistant",
            content="",
            structured_output={**card, "status": "pending"},
        )
        confirm_msg.id = uuid.uuid4()

        class _FakeScalarResult:
            def scalar_one_or_none(self):
                return confirm_msg

        db = MagicMock()
        db.execute = AsyncMock(return_value=_FakeScalarResult())
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.add = MagicMock()

        session = MagicMock(spec=ChatSession)
        session.id = session_uuid

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"success": True}))
        with patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call):
            approve_events = [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="approve",
                    user_id=user_id,
                    tenant_id=tenant_id,
                    write_confirm={"action": "approve", "confirmation_id": str(confirm_msg.id)},
                )
            ]

        mock_execute_tool_call.assert_not_called()
        error_events = [e for e in approve_events if e.get("type") == "error"]
        assert error_events
        assert "closed" in error_events[0]["error"].lower()
        assert confirm_msg.structured_output["status"] == "pending"
