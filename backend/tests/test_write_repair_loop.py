import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.write_validator import ValidationResult


class _Loop:
    """Exercises the repair-decision helper in isolation."""

    def __init__(self):
        from app.services.chat.agents.base_agent import WriteRepairState

        self.state = WriteRepairState(max_attempts=2)


def test_first_failure_requests_repair():
    from app.services.chat.agents.base_agent import WriteRepairState

    state = WriteRepairState(max_attempts=2)
    result = ValidationResult(ok=False, missing_required=["subsidiary"])
    assert state.should_repair("customer", result) is True
    assert state.exit_reason_for("customer") is None


def test_identical_failure_twice_exits_stall():
    from app.services.chat.agents.base_agent import WriteRepairState

    state = WriteRepairState(max_attempts=2)
    result = ValidationResult(ok=False, missing_required=["subsidiary"])
    state.should_repair("customer", result)
    assert state.should_repair("customer", result) is False
    assert state.exit_reason_for("customer") == "stall"


def test_budget_exhausts_after_max_attempts():
    from app.services.chat.agents.base_agent import WriteRepairState

    state = WriteRepairState(max_attempts=2)
    state.should_repair("customer", ValidationResult(ok=False, missing_required=["a"]))
    state.should_repair("customer", ValidationResult(ok=False, missing_required=["b"]))
    assert state.should_repair("customer", ValidationResult(ok=False, missing_required=["c"])) is False
    assert state.exit_reason_for("customer") == "budget"


def test_success_records_done():
    from app.services.chat.agents.base_agent import WriteRepairState

    state = WriteRepairState(max_attempts=2)
    state.should_repair("customer", ValidationResult(ok=False, missing_required=["a"]))
    assert state.should_repair("customer", ValidationResult(ok=True)) is False
    assert state.exit_reason_for("customer") == "done"


def test_state_is_per_record_type():
    from app.services.chat.agents.base_agent import WriteRepairState

    state = WriteRepairState(max_attempts=2)
    r = ValidationResult(ok=False, missing_required=["subsidiary"])
    state.should_repair("customer", r)
    assert state.should_repair("invoice", r) is True


def test_stall_detection_still_works_within_one_repair_cycle():
    """Regression guard for the budget-reset fix below: resetting a
    record_type's attempts/fingerprint on exit must not weaken stall
    detection WITHIN a still-open cycle. The fingerprint comparison that
    detects a stall runs BEFORE any reset — against the fingerprint left by
    the previous call in this same cycle — so two identical failures in a
    row for the same record_type must still exit "stall" with budget to
    spare (1 of 2 attempts used), not be silently granted a third attempt."""
    from app.services.chat.agents.base_agent import WriteRepairState

    state = WriteRepairState(max_attempts=2)
    result = ValidationResult(ok=False, missing_required=["subsidiary"])
    assert state.should_repair("journalEntry", result) is True
    assert state.should_repair("journalEntry", result) is False
    assert state.exit_reason_for("journalEntry") == "stall"


def test_second_distinct_write_of_same_record_type_gets_its_own_budget():
    """The reported bug: the model creates a journalEntry and exhausts its 2
    repair attempts (-> budget), then proposes a second, unrelated
    journalEntry missing a DIFFERENT field later in the SAME turn. Without
    resetting the budget on exit, the second write would inherit the first
    write's exhausted attempt count and get zero repair attempts of its own.
    """
    from app.services.chat.agents.base_agent import WriteRepairState

    state = WriteRepairState(max_attempts=2)
    assert state.should_repair("journalEntry", ValidationResult(ok=False, missing_required=["subsidiary"])) is True
    assert state.should_repair("journalEntry", ValidationResult(ok=False, missing_required=["account"])) is True
    # Third DISTINCT failure for the same cycle -> budget exhausted.
    assert state.should_repair("journalEntry", ValidationResult(ok=False, missing_required=["memo"])) is False
    assert state.exit_reason_for("journalEntry") == "budget"

    # A second, unrelated journalEntry write, later in the same turn,
    # missing a completely different field -> must get its own fresh budget,
    # not immediately exit "budget" again with zero attempts granted.
    assert state.should_repair("journalEntry", ValidationResult(ok=False, missing_required=["currency"])) is True
    # That second write's cycle can still reach its own, independent exit —
    # proving the state isn't just permanently pinned to "budget".
    assert state.should_repair("journalEntry", ValidationResult(ok=True)) is False
    assert state.exit_reason_for("journalEntry") == "done"


def test_exit_reason_for_one_record_type_is_not_polluted_by_another():
    """exit_reason_for() is scoped per record_type. Record type A exits
    "stall"; record type B is touched later in the same turn and is still
    mid-repair (never exited). B's own exit reason must read None, not A's
    stale "stall" bleeding across types."""
    from app.services.chat.agents.base_agent import WriteRepairState

    state = WriteRepairState(max_attempts=2)
    stall_result = ValidationResult(ok=False, missing_required=["subsidiary"])
    state.should_repair("journalEntry", stall_result)
    assert state.should_repair("journalEntry", stall_result) is False
    assert state.exit_reason_for("journalEntry") == "stall"

    # record type B has never been touched -- must not see A's reason.
    assert state.exit_reason_for("customer") is None

    # B enters its own repair cycle and is granted an attempt -- still
    # mid-cycle, hasn't exited yet.
    assert state.should_repair("customer", ValidationResult(ok=False, missing_required=["email"])) is True
    assert state.exit_reason_for("customer") is None
    # A's reason is unchanged by B's activity.
    assert state.exit_reason_for("journalEntry") == "stall"


# ---------------------------------------------------------------------------
# Integration: the repair loop wired into run_streaming's mutation intercept.
#
# WriteRepairState in isolation (above) cannot catch a wiring bug in the
# intercept block itself. run_streaming's ONLY consumer (orchestrator.py)
# does `async for event_type, payload in agent.run_streaming(...)` — every
# yield in the method is a 2-tuple. These tests drive run_streaming end to
# end with a mocked adapter so a yield shape mismatch fails loudly here
# instead of only in production.
# ---------------------------------------------------------------------------

_HEX_32 = "a1b2c3d4e5f67890a1b2c3d4e5f67890"


def _ext(tool_name: str) -> str:
    return f"ext__{_HEX_32}__{tool_name}"


def _make_agent():
    from app.services.chat.agents.base_agent import BaseSpecialistAgent

    class _RepairLoopTestAgent(BaseSpecialistAgent):
        agent_name = "test_repair"
        max_steps = 3

        @property
        def system_prompt(self):
            return "test prompt"

        @property
        def tool_definitions(self):
            return [
                {
                    "name": _ext("ns_createRecord"),
                    "description": "create a record",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ]

    agent = _RepairLoopTestAgent.__new__(_RepairLoopTestAgent)
    agent.tenant_id = uuid.uuid4()
    agent.user_id = uuid.uuid4()
    agent.correlation_id = "test-corr"
    return agent


@pytest.mark.asyncio
async def test_repair_loop_feeds_error_back_without_crashing_the_stream():
    """A validation failure must be fed back to the model as a 2-tuple event
    stream, exactly like every other branch in run_streaming — not crash the
    generator, and not show a human an invalid payload."""
    from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock
    from app.services.chat.record_metadata_service import FieldSpec, RecordMetadata

    agent = _make_agent()
    tool_name = _ext("ns_createRecord")
    metadata_tool_name = _ext("ns_getRecordTypeMetadata")

    # The investigation gate (requirement A) bounces a create with no prior
    # same-turn metadata call before it ever reaches validation — satisfy it
    # first so this attempt exercises the REPAIR loop this test is about.
    metadata_call_response = LLMResponse(
        text_blocks=[],
        tool_use_blocks=[ToolUseBlock(id="m1", name=metadata_tool_name, input={"recordType": "customer"})],
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )
    tool_call_response = LLMResponse(
        text_blocks=[],
        tool_use_blocks=[
            ToolUseBlock(
                id="t1",
                name=tool_name,
                # "customer" is required by the mocked metadata below but
                # missing here — this must fail validation.
                input={"recordType": "customer", "body": {"companyName": "Acme"}},
            )
        ],
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )
    final_response = LLMResponse(
        text_blocks=["Understood, I need the subsidiary."],
        tool_use_blocks=[],
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )
    # The prose-instead-of-proposing guard now fires on this turn, and that is
    # correct: a write was attempted, validation rejected it, no card ever
    # reached the human, and the model then answered in chat. That is the exact
    # live failure the guard exists for (staging 2026-08-28) — the operator is
    # told "I need the subsidiary" with nothing to act on.
    #
    # Before `_write_reached_the_human`, the guard stood down here because a
    # write tool HAD been called, so this test only needed three responses. It
    # now costs one bounce, hence the fourth. The turn still ends on the same
    # sentence, which is what this test is actually about.
    responses = iter([metadata_call_response, tool_call_response, final_response, final_response])

    async def _fake_stream_message(**kwargs):
        yield "response", next(responses)

    async def _fake_create_message(**kwargs):
        """Stage 2's forced hop. Returns the model DECLINING, so this test
        keeps exercising the repair loop rather than the forcing path — and
        so it fails loudly if the hop is ever reached with no stub, instead of
        silently swallowing a TypeError off a MagicMock."""
        from app.services.chat.agents.base_agent import _DECLINE_WRITE_TOOL

        return LLMResponse(
            text_blocks=[],
            tool_use_blocks=[ToolUseBlock(id="decline", name=_DECLINE_WRITE_TOOL, input={})],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )

    mock_adapter = MagicMock()
    mock_adapter.stream_message = _fake_stream_message
    mock_adapter.create_message = _fake_create_message
    mock_adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})
    mock_adapter.build_tool_result_message = MagicMock(
        return_value={"role": "user", "content": [{"type": "tool_result"}]}
    )

    metadata = RecordMetadata(
        record_type="customer",
        fields=[FieldSpec(name="subsidiary", label="Subsidiary", required=True)],
    )

    with (
        patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
        patch(
            "app.services.chat.write_validation.get_record_metadata",
            new_callable=AsyncMock,
            return_value=metadata,
        ),
        patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new_callable=AsyncMock),
        patch(
            "app.services.chat.agents.base_agent.extract_structured_confidence",
            new_callable=AsyncMock,
            return_value=MagicMock(score=4, source="mock"),
        ),
        patch(
            "app.services.chat.tools.execute_tool_call",
            new_callable=AsyncMock,
            return_value="{}",
        ),
    ):
        events = []
        async for event_type, payload in agent.run_streaming(
            task="Create a new customer record for Acme.",
            context={},
            db=AsyncMock(),
            adapter=mock_adapter,
            model="test-model",
        ):
            # The bug this regression test catches: a non-2-tuple yield
            # raises ValueError right here, at unpack time.
            events.append((event_type, payload))

    tool_ends = [p for t, p in events if t == "tool_end" and p.get("tool_name") == tool_name]
    assert len(tool_ends) == 1
    assert tool_ends[0]["success"] is False
    # The summary must name what actually failed — diagnosable after the
    # fact from the tool-call log, not a generic "something went wrong".
    assert "subsidiary" in tool_ends[0]["result_summary"]

    responses_events = [p for t, p in events if t == "response"]
    assert len(responses_events) == 1
    assert responses_events[0].data == "Understood, I need the subsidiary."

    # The repair budget was consumed (attempt 1 of 2) but the loop has not
    # exited terminally — it fed the error back and let the model retry.
    assert agent._write_repair.exit_reason_for("customer") is None

    # _last_validation is only stashed on the fall-through path (Task 7's
    # consumer); the repair `continue` must skip it for a rejected payload.
    assert not hasattr(agent, "_last_validation")


@pytest.mark.asyncio
async def test_persisted_log_flags_a_card_shown_after_repair_is_exhausted():
    """Audit-log diagnosability (Task 7 follow-up, from Task 6's reviewer).

    When should_repair() gives up — exit_reason "stall" or "budget", not
    "done" — execution falls through the repair branch entirely and reaches
    the pre-existing confirmation path, which logs a generic "Confirmation
    required" entry with success=True. In the PERSISTED tool_calls_log that
    is indistinguishable from a payload that validated cleanly on the first
    attempt. This test drives the same invalid payload twice (identical
    fingerprint -> "stall" on the second attempt) and asserts the persisted
    log entry for the card names what was still wrong when it was shown.
    """
    from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock
    from app.services.chat.record_metadata_service import FieldSpec, RecordMetadata

    agent = _make_agent()
    tool_name = _ext("ns_createRecord")
    metadata_tool_name = _ext("ns_getRecordTypeMetadata")

    # The investigation gate (requirement A) bounces a create with no prior
    # same-turn metadata call before it ever reaches validation — satisfy it
    # first so both attempts below exercise the REPAIR loop this test is
    # about, not the investigation gate.
    metadata_call_response = LLMResponse(
        text_blocks=[],
        tool_use_blocks=[ToolUseBlock(id="m1", name=metadata_tool_name, input={"recordType": "customer"})],
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )

    # Same missing field both times -> identical ValidationResult fingerprint,
    # so the SECOND should_repair() call exits "stall" instead of granting a
    # third attempt, and falls through to build the confirmation card.
    bad_input = {"recordType": "customer", "body": {"companyName": "Acme"}}
    tool_call_response_1 = LLMResponse(
        text_blocks=[],
        tool_use_blocks=[ToolUseBlock(id="t1", name=tool_name, input=bad_input)],
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )
    tool_call_response_2 = LLMResponse(
        text_blocks=[],
        tool_use_blocks=[ToolUseBlock(id="t2", name=tool_name, input=bad_input)],
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )
    final_response = LLMResponse(
        text_blocks=["Here is the confirmation."],
        tool_use_blocks=[],
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )
    responses = iter([metadata_call_response, tool_call_response_1, tool_call_response_2, final_response])

    async def _fake_stream_message(**kwargs):
        yield "response", next(responses)

    mock_adapter = MagicMock()
    mock_adapter.stream_message = _fake_stream_message
    mock_adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})
    mock_adapter.build_tool_result_message = MagicMock(
        return_value={"role": "user", "content": [{"type": "tool_result"}]}
    )

    metadata = RecordMetadata(
        record_type="customer",
        fields=[FieldSpec(name="subsidiary", label="Subsidiary", required=True)],
    )

    with (
        patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
        patch(
            "app.services.chat.write_validation.get_record_metadata",
            new_callable=AsyncMock,
            return_value=metadata,
        ),
        patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new_callable=AsyncMock),
        patch(
            "app.services.chat.agents.base_agent.extract_structured_confidence",
            new_callable=AsyncMock,
            return_value=MagicMock(score=4, source="mock"),
        ),
        patch(
            "app.services.chat.tools.execute_tool_call",
            new_callable=AsyncMock,
            return_value="{}",
        ),
    ):
        events = []
        async for event_type, payload in agent.run_streaming(
            task="Create a new customer record for Acme.",
            context={},
            db=AsyncMock(),
            adapter=mock_adapter,
            model="test-model",
        ):
            events.append((event_type, payload))

    # Precondition: repair really did exhaust via "stall", not "done".
    assert agent._write_repair.exit_reason_for("customer") == "stall"
    assert agent._last_validation is not None
    assert agent._last_validation.ok is False

    responses_events = [p for t, p in events if t == "response"]
    assert len(responses_events) == 1
    tool_calls_log = responses_events[0].tool_calls_log
    # The metadata-satisfying call is entry 0; the repair-request and the
    # eventual card are entries 1 and 2.
    assert len(tool_calls_log) == 3

    _metadata_entry, repair_entry, card_entry = tool_calls_log
    # The first (repair-requested) entry is untouched by this change.
    assert "validation_failed_before_confirmation" not in repair_entry
    # The second entry is the card shown despite repair giving up — the
    # persisted log must name what was still wrong, not just "Confirmation
    # required" (which is indistinguishable from a clean first attempt).
    assert "validation_failed_before_confirmation" in card_entry
    assert "subsidiary" in card_entry["validation_failed_before_confirmation"]
