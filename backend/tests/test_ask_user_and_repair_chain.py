"""Tests for the ask_user server-authority slot mechanism (agentic-repair
design requirement C) and the repair-chain stamping + intent guard
(requirements D/E), as wired into base_agent.py's mutation intercept.

Drives the real `agent.run_streaming` loop end to end (same harness as
`test_write_repair_loop.py` / `test_write_investigation_gate.py`) so a
wiring bug in the intercept itself — not just the resolver function in
isolation (already covered by `test_slot_option_sources.py`) — fails here.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HEX_32 = "a1b2c3d4e5f67890a1b2c3d4e5f67890"


def _ext(tool_name: str) -> str:
    return f"ext__{_HEX_32}__{tool_name}"


def _make_agent():
    from app.services.chat.agents.base_agent import BaseSpecialistAgent

    class _AskUserTestAgent(BaseSpecialistAgent):
        agent_name = "test_ask_user"
        max_steps = 4

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
                },
                {
                    "name": _ext("ns_getRecordTypeMetadata"),
                    "description": "get record type metadata",
                    "input_schema": {"type": "object", "properties": {}},
                },
            ]

    agent = _AskUserTestAgent.__new__(_AskUserTestAgent)
    agent.tenant_id = uuid.uuid4()
    agent.user_id = uuid.uuid4()
    agent.correlation_id = "test-corr"
    return agent


def _make_adapter(responses):
    responses_iter = iter(responses)

    async def _fake_stream_message(**kwargs):
        yield "response", next(responses_iter)

    mock_adapter = MagicMock()
    mock_adapter.stream_message = _fake_stream_message
    mock_adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})
    mock_adapter.build_tool_result_message = MagicMock(
        return_value={"role": "user", "content": [{"type": "tool_result"}]}
    )
    return mock_adapter


def _metadata():
    from app.services.chat.record_metadata_service import FieldSpec, RecordMetadata

    return RecordMetadata(
        record_type="customer",
        fields=[
            FieldSpec(name="subsidiary", label="Primary Subsidiary", type="select"),
            FieldSpec(name="companyname", label="Company Name", type="text"),
        ],
        requirements_known=False,
    )


@contextlib.contextmanager
def _patches(*, subsidiaries_response=None, generic_execute_result="{}"):
    """metadata mocked identically at BOTH call sites the intercept uses:
    write_validation.get_record_metadata (validate_mutation's internal
    call) and record_metadata_service.get_record_metadata (the intercept's
    OWN direct call for ask_user name-verification — a second, separately
    mockable call the production cache makes cheap, see design "asking").
    """
    metadata = _metadata()
    subsidiaries_response = (
        subsidiaries_response
        if subsidiaries_response is not None
        else json.dumps({"subsidiaries": [{"id": "1", "name": "Framework Inc"}]})
    )

    async def _fake_execute_tool_call(**kwargs):
        tool_name = kwargs.get("tool_name", "")
        if tool_name.endswith("ns_getSubsidiaries"):
            return subsidiaries_response
        return generic_execute_result

    with (
        patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
        patch(
            "app.services.chat.write_validation.get_record_metadata",
            new_callable=AsyncMock,
            return_value=metadata,
        ),
        patch(
            "app.services.chat.record_metadata_service.get_record_metadata",
            new_callable=AsyncMock,
            return_value=metadata,
        ),
        patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new_callable=AsyncMock),
        patch(
            "app.services.chat.agents.base_agent.extract_structured_confidence",
            new_callable=AsyncMock,
            return_value=MagicMock(score=4, source="mock"),
        ),
        patch("app.services.chat.tools.execute_tool_call", new_callable=AsyncMock, side_effect=_fake_execute_tool_call),
        patch(
            "app.services.chat.slot_option_sources.execute_tool_call",
            new_callable=AsyncMock,
            side_effect=_fake_execute_tool_call,
        ),
    ):
        yield


async def _run(agent, adapter, task="Create a new customer record for Acme."):
    events = []
    async for event_type, payload in agent.run_streaming(
        task=task,
        context={},
        db=AsyncMock(),
        adapter=adapter,
        model="test-model",
    ):
        events.append((event_type, payload))
    return events


def _metadata_step(metadata_tool_name, record_type="customer"):
    from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

    return LLMResponse(
        text_blocks=[],
        tool_use_blocks=[ToolUseBlock(id="m1", name=metadata_tool_name, input={"recordType": record_type})],
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )


def _final_step():
    from app.services.chat.llm_adapter import LLMResponse, TokenUsage

    return LLMResponse(text_blocks=["done"], tool_use_blocks=[], usage=TokenUsage(10, 10))


# ---------------------------------------------------------------------------
# (C) ask_user — server-authority slot resolution
# ---------------------------------------------------------------------------


class TestAskUserResolution:
    @pytest.mark.asyncio
    async def test_registered_hint_becomes_editable_slot_on_the_card(self):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        metadata_name = _ext("ns_getRecordTypeMetadata")

        create_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1",
                    name=create_name,
                    input={
                        "recordType": "customer",
                        "body": {"companyname": "Acme"},
                        "ask_user": ["subsidiary"],
                    },
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([_metadata_step(metadata_name), create_call, _final_step()])

        with _patches():
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert len(confirmations) == 1
        slots = confirmations[0]["editable_slots"]
        assert len(slots) == 1
        assert slots[0]["name"] == "subsidiary"
        assert slots[0]["allowed"] == [{"value": "1", "label": "Framework Inc"}]

    @pytest.mark.asyncio
    async def test_ask_user_key_never_reaches_tool_input_on_the_card(self):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        metadata_name = _ext("ns_getRecordTypeMetadata")

        create_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1",
                    name=create_name,
                    input={
                        "recordType": "customer",
                        "body": {"companyname": "Acme"},
                        "ask_user": ["subsidiary"],
                    },
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([_metadata_step(metadata_name), create_call, _final_step()])

        with _patches():
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert "ask_user" not in confirmations[0]["tool_input"]

    @pytest.mark.asyncio
    async def test_name_not_in_metadata_yields_no_slot_and_feedback(self):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        metadata_name = _ext("ns_getRecordTypeMetadata")

        create_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1",
                    name=create_name,
                    input={
                        "recordType": "customer",
                        "body": {"companyname": "Acme", "subsidiary": "1"},
                        "ask_user": ["not_a_real_field"],
                    },
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        final = _final_step()
        adapter = _make_adapter([_metadata_step(metadata_name), create_call, final])

        with _patches():
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert len(confirmations) == 1
        assert confirmations[0]["editable_slots"] == []

        # The model must be told its hint didn't resolve — reachable via the
        # persisted trail (summarize_tool_result falls back to the raw JSON
        # for a shape with no row/count key, so the marker survives intact).
        responses_events = [p for t, p in events if t == "response"]
        tool_calls_log = responses_events[0].tool_calls_log
        card_entry = tool_calls_log[-1]
        assert "not_a_real_field" in card_entry["result_summary"]
        assert "unresolved_ask_user_fields" in card_entry["result_summary"]

    @pytest.mark.asyncio
    async def test_registered_but_zero_options_yields_no_slot(self):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        metadata_name = _ext("ns_getRecordTypeMetadata")

        create_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1",
                    name=create_name,
                    input={
                        "recordType": "customer",
                        "body": {"companyname": "Acme", "subsidiary": "1"},
                        "ask_user": ["subsidiary"],
                    },
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([_metadata_step(metadata_name), create_call, _final_step()])

        with _patches(subsidiaries_response=json.dumps({"subsidiaries": []})):
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert confirmations[0]["editable_slots"] == []

    @pytest.mark.asyncio
    async def test_malformed_ask_user_hint_never_raises(self):
        """A non-list ask_user value is malformed model output — must be
        handled gracefully, not crash the turn."""
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        metadata_name = _ext("ns_getRecordTypeMetadata")

        create_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1",
                    name=create_name,
                    input={
                        "recordType": "customer",
                        "body": {"companyname": "Acme", "subsidiary": "1"},
                        "ask_user": "subsidiary",
                    },
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([_metadata_step(metadata_name), create_call, _final_step()])

        with _patches():
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert len(confirmations) == 1
        assert confirmations[0]["editable_slots"] == []


# ---------------------------------------------------------------------------
# (D)/(E) repair-chain stamping + intent guard
# ---------------------------------------------------------------------------


class TestRepairChainStamping:
    @pytest.mark.asyncio
    async def test_normal_turn_card_has_no_repair_of(self):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        metadata_name = _ext("ns_getRecordTypeMetadata")

        create_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1",
                    name=create_name,
                    input={"recordType": "customer", "body": {"companyname": "Acme", "subsidiary": "1"}},
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([_metadata_step(metadata_name), create_call, _final_step()])

        with _patches():
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert confirmations[0]["repair_of"] is None
        assert confirmations[0]["repair_attempt"] == 0

    @pytest.mark.asyncio
    async def test_matching_repair_context_stamps_the_card(self):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        agent._write_repair_context = {
            "root_id": "11111111-1111-1111-1111-111111111111",
            "attempt": 2,
            "mutation_type": "create",
            "record_type": "customer",
        }
        create_name = _ext("ns_createRecord")
        metadata_name = _ext("ns_getRecordTypeMetadata")

        create_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1",
                    name=create_name,
                    input={"recordType": "customer", "body": {"companyname": "Acme", "subsidiary": "1"}},
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([_metadata_step(metadata_name), create_call, _final_step()])

        with _patches():
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert confirmations[0]["repair_of"] == "11111111-1111-1111-1111-111111111111"
        assert confirmations[0]["repair_attempt"] == 2

    @pytest.mark.asyncio
    async def test_mismatched_record_type_is_not_chained_intent_guard(self):
        """The intent guard (E): a repair-turn proposal whose
        (mutation_type, record_type) differs from the root must NOT be
        linked into the chain — it becomes a plainly fresh proposal."""
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        agent._write_repair_context = {
            "root_id": "11111111-1111-1111-1111-111111111111",
            "attempt": 1,
            "mutation_type": "create",
            "record_type": "customer",
        }
        create_name = _ext("ns_createRecord")
        metadata_name = _ext("ns_getRecordTypeMetadata")

        # The model proposes a DIFFERENT record type this time.
        create_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1", name=create_name, input={"recordType": "invoice", "body": {"memo": "x", "entity": "1"}}
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([_metadata_step(metadata_name, record_type="invoice"), create_call, _final_step()])

        with _patches():
            events = await _run(agent, adapter, task="Create a customer, actually create an invoice instead.")

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert confirmations[0]["repair_of"] is None
        assert confirmations[0]["repair_attempt"] == 0

    @pytest.mark.asyncio
    async def test_mismatched_mutation_type_is_not_chained(self):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        agent._write_repair_context = {
            "root_id": "11111111-1111-1111-1111-111111111111",
            "attempt": 1,
            "mutation_type": "update",
            "record_type": "customer",
        }
        create_name = _ext("ns_createRecord")
        metadata_name = _ext("ns_getRecordTypeMetadata")

        create_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1",
                    name=create_name,
                    input={"recordType": "customer", "body": {"companyname": "Acme", "subsidiary": "1"}},
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([_metadata_step(metadata_name), create_call, _final_step()])

        with _patches():
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert confirmations[0]["repair_of"] is None
        assert confirmations[0]["repair_attempt"] == 0

    @pytest.mark.asyncio
    async def test_model_supplied_repair_of_in_tool_input_is_ignored(self):
        """Server-stamped only — a model trying to inject repair_of/attempt
        directly into tool_input must have zero effect on the card's chain
        fields."""
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        metadata_name = _ext("ns_getRecordTypeMetadata")

        create_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1",
                    name=create_name,
                    input={
                        "recordType": "customer",
                        "body": {"companyname": "Acme", "subsidiary": "1"},
                        "repair_of": "attacker-supplied-root-id",
                        "repair_attempt": 99,
                    },
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([_metadata_step(metadata_name), create_call, _final_step()])

        with _patches():
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert confirmations[0]["repair_of"] is None
        assert confirmations[0]["repair_attempt"] == 0


# ---------------------------------------------------------------------------
# ask_user vs. the repair loop — which of the two gets a missing required field
# ---------------------------------------------------------------------------


class TestDelegationVersusRepair:
    """The registry made `customer` really validate, which put these two
    mechanisms in direct contact for the first time.

    A missing required field goes to exactly one of two places, and the
    `ask_user` hint is the switch. Resolved hint -> the human, via a slot on
    the card. No hint, or a hint the server could not resolve -> back to the
    model, via the bounded repair loop. Getting this backwards is not cosmetic:
    bouncing a field the model has already said it cannot determine burns the
    repair budget to a stall, and carding a field with no slot produces a
    proposal the approve path's slot-coverage gate refuses — a dead end for
    the operator either way.

    (The fixtures in the classes above carry complete payloads precisely so
    that they test hint resolution and repair-chain stamping WITHOUT this
    interaction as a hidden variable. It is tested here instead.)
    """

    @staticmethod
    def _create_call(create_name, **extra):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        return LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1",
                    name=create_name,
                    # subsidiary deliberately absent — the registry makes it
                    # required, so this payload is genuinely incomplete.
                    input={"recordType": "customer", "body": {"companyname": "Acme"}, **extra},
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )

    @staticmethod
    def _repair_bounced(events):
        return [p for t, p in events if t == "tool_end" and "Write repair requested" in (p.get("result_summary") or "")]

    @pytest.mark.asyncio
    async def test_incomplete_payload_with_no_hint_goes_to_the_model(self):
        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        adapter = _make_adapter(
            [_metadata_step(_ext("ns_getRecordTypeMetadata")), self._create_call(create_name), _final_step()]
        )

        with _patches():
            events = await _run(agent, adapter)

        assert [p for t, p in events if t == "confirmation_required"] == []
        assert len(self._repair_bounced(events)) == 1

    @pytest.mark.asyncio
    async def test_incomplete_payload_with_a_resolving_hint_goes_to_the_human(self):
        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        adapter = _make_adapter(
            [
                _metadata_step(_ext("ns_getRecordTypeMetadata")),
                self._create_call(create_name, ask_user=["subsidiary"]),
                _final_step(),
            ]
        )

        with _patches():
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert len(confirmations) == 1
        assert self._repair_bounced(events) == []
        slots = confirmations[0]["editable_slots"]
        assert [s["name"] for s in slots] == ["subsidiary"]
        # The slot the human sees carries SERVER-fetched options — not the
        # bare, optionless slot validate_write derives for a missing field.
        assert slots[0]["allowed"] == [{"value": "1", "label": "Framework Inc"}]

    @pytest.mark.asyncio
    async def test_incomplete_payload_with_an_unknown_hint_goes_to_the_model(self):
        """The model asked about a field that does not exist, so nothing was
        delegated and the real gap remains. Carding it would hand the operator
        a proposal they cannot approve."""
        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        adapter = _make_adapter(
            [
                _metadata_step(_ext("ns_getRecordTypeMetadata")),
                self._create_call(create_name, ask_user=["not_a_real_field"]),
                _final_step(),
            ]
        )

        with _patches():
            events = await _run(agent, adapter)

        assert [p for t, p in events if t == "confirmation_required"] == []
        assert len(self._repair_bounced(events)) == 1

    @pytest.mark.asyncio
    async def test_a_bounced_attempt_still_tells_the_model_why_its_hint_failed(self):
        """T2 gate finding: the card path reports rejected hints, the bounce
        path dropped them. A model bounced with no explanation repeats the same
        bad field name and drains its repair budget on a mistake we already
        diagnosed."""
        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        adapter = _make_adapter(
            [
                _metadata_step(_ext("ns_getRecordTypeMetadata")),
                self._create_call(create_name, ask_user=["not_a_real_field"]),
                _final_step(),
            ]
        )

        with _patches():
            events = await _run(agent, adapter)

        responses = [p for t, p in events if t == "response"]
        bounce_entry = responses[0].tool_calls_log[-1]
        assert "unresolved_ask_user_fields" in bounce_entry["result_summary"]
        assert "not_a_real_field" in bounce_entry["result_summary"]

    @pytest.mark.asyncio
    async def test_incomplete_payload_with_zero_options_goes_to_the_model(self):
        """The hint named a registered field, but the server found nothing to
        offer. An empty dropdown is not an answerable question."""
        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        adapter = _make_adapter(
            [
                _metadata_step(_ext("ns_getRecordTypeMetadata")),
                self._create_call(create_name, ask_user=["subsidiary"]),
                _final_step(),
            ]
        )

        with _patches(subsidiaries_response=json.dumps({"subsidiaries": []})):
            events = await _run(agent, adapter)

        assert [p for t, p in events if t == "confirmation_required"] == []
        assert len(self._repair_bounced(events)) == 1

    @pytest.mark.asyncio
    async def test_hint_for_a_field_already_supplied_still_offers_the_choice(self):
        """A complete payload plus a hint is the model saying "I picked one,
        but let them change it". That must reach the card as a real choice,
        not be swallowed because nothing was technically missing."""
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1",
                    name=create_name,
                    input={
                        "recordType": "customer",
                        "body": {"companyname": "Acme", "subsidiary": "1"},
                        "ask_user": ["subsidiary"],
                    },
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([_metadata_step(_ext("ns_getRecordTypeMetadata")), call, _final_step()])

        with _patches():
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert len(confirmations) == 1
        assert [s["name"] for s in confirmations[0]["editable_slots"]] == ["subsidiary"]
