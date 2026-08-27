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
    async def test_missing_required_field_with_options_is_asked_not_bounced(self):
        """BEHAVIOUR CHANGE, deliberate. This test previously asserted the
        opposite: no hint meant the proposal bounced to the model's repair
        budget.

        Bouncing was wrong for THIS class of field. When the server already
        knows the field is required AND already holds a server-fetched list of
        valid values, there is nothing for the model to discover — it can only
        guess, and a guess on a financial write is exactly what the card
        exists to prevent. Worse, the model demonstrably does not ask: live on
        staging it either narrated the options in prose or called
        ns_selector_app, announcing a picker the UI cannot render. Waiting for
        `ask_user` made the form unreachable in practice.

        So the server declares the slot itself. The model keeps its repair
        budget for gaps only IT can close (see the sibling test below)."""
        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        adapter = _make_adapter(
            [_metadata_step(_ext("ns_getRecordTypeMetadata")), self._create_call(create_name), _final_step()]
        )

        with _patches():
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert len(confirmations) == 1, "a pickable required field must reach the human, not the repair loop"
        assert self._repair_bounced(events) == []
        slots = confirmations[0]["editable_slots"]
        assert [s["name"] for s in slots] == ["subsidiary"]
        # The slot carries the SERVER-fetched allow-set, so the card renders a
        # real dropdown rather than a bare text box asking for an internal id.
        assert slots[0]["allowed"] == [{"value": "1", "label": "Framework Inc"}]

    @pytest.mark.asyncio
    async def test_missing_required_field_without_options_still_bounces(self):
        """The repair loop is not obsolete — it still owns every gap the human
        cannot simply pick from a list. `companyname` has no option source, so
        there is no allow-set to offer and the model must resolve it."""
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1",
                    name=create_name,
                    # subsidiary supplied, company name absent: the missing
                    # field is the one with no server-side option source.
                    input={"recordType": "customer", "body": {"subsidiary": "1"}},
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([_metadata_step(_ext("ns_getRecordTypeMetadata")), call, _final_step()])

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
    async def test_unknown_hint_is_reported_but_does_not_suppress_a_real_slot(self):
        """The model asked about a field that does not exist. That hint is
        rejected and reported back — but it must not cost the human the slot
        the SERVER independently knows is needed.

        This previously bounced to the repair loop, because the model's hint
        was the ONLY thing that could produce a slot. Now the server declares
        `subsidiary` on its own, so a useless hint degrades to feedback rather
        than to a dead end for the operator."""
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

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert len(confirmations) == 1
        assert [s["name"] for s in confirmations[0]["editable_slots"]] == ["subsidiary"]
        # The model still learns its hint was junk, so it does not repeat it.
        responses = [p for t, p in events if t == "response"]
        assert "not_a_real_field" in responses[0].tool_calls_log[-1]["result_summary"]

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


class TestSelectorAppRedirectReachesTheForm:
    """The redirect is only worth anything if it ends at the slot form.

    Measured on staging: 3 of 5 "create a customer" attempts called
    ns_selector_app and told the operator a picker had opened, when nothing
    had. This proves the whole chain — selector call intercepted, model told
    which call to make instead, next proposal carrying ask_user, card rendered
    with a server-fetched dropdown.
    """

    @pytest.mark.asyncio
    async def test_selector_call_is_redirected_and_then_reaches_the_card(self):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        selector_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[ToolUseBlock(id="s1", name=_ext("ns_selector_app"), input={"recordType": "subsidiary"})],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        # What the model does after being told the selector is unavailable.
        retry = LLMResponse(
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
        adapter = _make_adapter([_metadata_step(_ext("ns_getRecordTypeMetadata")), selector_call, retry, _final_step()])

        with _patches():
            events = await _run(agent, adapter)

        # The selector was answered, never dispatched to NetSuite.
        selector_ends = [
            p
            for t, p in events
            if t == "tool_end" and "Selector app is not available" in (p.get("result_summary") or "")
        ]
        assert len(selector_ends) == 1

        # And the turn ends where it should: a card with a real dropdown.
        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert len(confirmations) == 1
        slots = confirmations[0]["editable_slots"]
        assert [s["name"] for s in slots] == ["subsidiary"]
        assert slots[0]["allowed"] == [{"value": "1", "label": "Framework Inc"}]

    @pytest.mark.asyncio
    async def test_the_redirect_tells_the_model_no_picker_was_shown(self):
        """The specific falsehood being corrected. Without this the model
        narrates "I've opened the selector" regardless, because that is what
        it believed it was doing."""
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        selector_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[ToolUseBlock(id="s1", name=_ext("ns_selector_app"), input={"recordType": "subsidiary"})],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([_metadata_step(_ext("ns_getRecordTypeMetadata")), selector_call, _final_step()])

        with _patches():
            events = await _run(agent, adapter)

        log = [p for t, p in events if t == "response"][0].tool_calls_log
        entry = next(e for e in log if "selector_app" in e["tool"])
        assert "selector_unavailable" in entry["result_summary"]
        assert "ask_user" in entry["result_summary"]


class TestProseInsteadOfProposing:
    """The failure every other mechanism on this branch cannot reach.

    Observed live three times: the model calls ns_getRecordTypeMetadata —
    declaring, in its own tool call, that it intends to write — and then
    answers in prose instead of proposing anything. The server-declared slot
    needs a proposal to attach to; the ns_selector_app redirect needs a
    selector call to intercept. Neither exists on this path, so the operator
    is asked a question in chat and the confirmation card never appears.

    The signal is the model's OWN behaviour, not a guess at intent: it looked
    up how to write a record type and then did not write one. Bounded to one
    re-entry per turn, exactly like the investigation gate and the existing
    step-0 query guard this mirrors.
    """

    @pytest.mark.asyncio
    async def test_metadata_then_prose_is_bounced_once(self):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        prose = LLMResponse(
            text_blocks=["Which subsidiary should this customer be created under?"],
            tool_use_blocks=[],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        proposal = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1",
                    name=_ext("ns_createRecord"),
                    input={"recordType": "customer", "body": {"companyname": "Acme"}},
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([_metadata_step(_ext("ns_getRecordTypeMetadata")), prose, proposal, _final_step()])

        with _patches():
            events = await _run(agent, adapter)

        # The prose turn did not end the conversation; the model was sent back
        # and its next proposal reached the card with a real dropdown.
        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert len(confirmations) == 1
        assert [s["name"] for s in confirmations[0]["editable_slots"]] == ["subsidiary"]

    @pytest.mark.asyncio
    async def test_prose_with_no_metadata_lookup_is_left_alone(self):
        """Not every text answer is a dodged write. A plain question, with no
        metadata lookup behind it, must answer normally — bouncing those would
        make ordinary conversation impossible."""
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage

        agent = _make_agent()
        prose = LLMResponse(
            text_blocks=["NetSuite subsidiaries represent legal entities."],
            tool_use_blocks=[],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([prose])

        with _patches():
            events = await _run(agent, adapter)

        responses = [p for t, p in events if t == "response"]
        assert "subsidiaries represent legal entities" in (responses[0].data or "")

    @pytest.mark.asyncio
    async def test_the_bounce_happens_at_most_once_per_turn(self):
        """A model that answers in prose twice must be allowed to finish, not
        loop. The bound is what makes this safe to put in the loop at all."""
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage

        agent = _make_agent()
        prose1 = LLMResponse(text_blocks=["Which subsidiary?"], tool_use_blocks=[], usage=TokenUsage(10, 10))
        prose2 = LLMResponse(text_blocks=["I still need the subsidiary."], tool_use_blocks=[], usage=TokenUsage(10, 10))
        adapter = _make_adapter([_metadata_step(_ext("ns_getRecordTypeMetadata")), prose1, prose2])

        with _patches():
            events = await _run(agent, adapter)

        responses = [p for t, p in events if t == "response"]
        assert "still need the subsidiary" in (responses[0].data or "")


class TestProseGuardDoesNotHijackQuestions:
    """T2 gate (branch round 1, major): the prose-instead-of-proposing guard
    fires on ANY turn that fetched record metadata and then answered in prose —
    including a turn where the user only ASKED something.

    "What fields does a NetSuite customer record require?" is answered by
    calling ns_getRecordTypeMetadata and replying in prose. That is the correct
    behaviour, and the guard as first written injected "Call the write tool
    NOW", coercing an unwanted write-confirmation card in reply to a question.

    There is no non-fuzzy signal for user intent available at that point — the
    guard deliberately triggers on the model's own tool calls, not on a guess
    about the user. So the fix is not a better trigger, it is a directive the
    model can correctly decline: it names both branches, and a wrongly-fired
    bounce costs one extra turn instead of producing a false write proposal.
    """

    @pytest.mark.asyncio
    async def test_the_directive_lets_the_model_decline(self):
        """The bounce must not be a command to write. It must be a reminder of
        the mechanism, conditional on the user having actually asked for one."""
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage

        agent = _make_agent()
        prose = LLMResponse(
            text_blocks=["A customer needs companyName and subsidiary."],
            tool_use_blocks=[],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        answer = LLMResponse(
            text_blocks=["A customer requires companyName and subsidiary."],
            tool_use_blocks=[],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([_metadata_step(_ext("ns_getRecordTypeMetadata")), prose, answer])

        with _patches():
            events = await _run(agent, adapter, task="What fields does a customer record require?")

        # No write was proposed, and the user still got their answer.
        assert [p for t, p in events if t == "confirmation_required"] == []
        responses = [p for t, p in events if t == "response"]
        assert "companyName" in (responses[0].data or "")

    @pytest.mark.asyncio
    async def test_the_directive_names_both_branches(self):
        """Read the injected text directly — this is the whole mitigation."""
        import inspect

        from app.services.chat.agents import base_agent as ba

        src = inspect.getsource(ba.BaseSpecialistAgent.run_streaming)
        start = src.index("_prose_instead_of_write_bounced = True")
        directive = src[start : start + 1400]
        assert "ask_user" in directive
        # Both branches must be named. An unconditional "call the write tool
        # NOW" is what hijacked informational questions; asserting merely that
        # the word "question" appears passes on the BROKEN wording too, so
        # require the explicit decline branch.
        assert "IF the user only asked a question" in directive
        assert "ignore all" in directive
