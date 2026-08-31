"""Tests for the investigation gate (agentic-repair design requirement A).

A create/upsert proposal reaching the mutation intercept with no prior
same-turn `ns_getRecordTypeMetadata` call for its record type is bounced back
to the model with a structured error — mechanism, not prompt (the write
profile prose has been ignored live 3x on this branch per the design). Bounded
by construction: at most ONE bounce per (turn, record_type), tracked in a
per-agent-instance set — a stubborn model's SECOND proposal always reaches
validation/the card.

Drives the real `agent.run_streaming` loop end to end (same harness as
`test_write_repair_loop.py`) so a wiring bug in the intercept itself — not
just the gate helper in isolation — fails here.
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

    class _GateTestAgent(BaseSpecialistAgent):
        agent_name = "test_gate"
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
                    "name": _ext("ns_upsertRecord"),
                    "description": "upsert a record",
                    "input_schema": {"type": "object", "properties": {}},
                },
                {
                    "name": _ext("ns_updateRecord"),
                    "description": "update a record",
                    "input_schema": {"type": "object", "properties": {}},
                },
                {
                    "name": _ext("ns_deleteRecord"),
                    "description": "delete a record",
                    "input_schema": {"type": "object", "properties": {}},
                },
                {
                    "name": _ext("ns_getRecordTypeMetadata"),
                    "description": "get record type metadata",
                    "input_schema": {"type": "object", "properties": {}},
                },
            ]

    agent = _GateTestAgent.__new__(_GateTestAgent)
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


@contextlib.contextmanager
def _patches(execute_tool_call_result="{}"):
    """The common patch set every test in this file needs. `metadata=None`
    (via write_validation.get_record_metadata) so a create that DOES pass
    the gate reaches a card in one attempt (unvalidated=True, ok=True per
    the honesty rule — no repair bounce in the way)."""
    with (
        patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
        patch(
            "app.services.chat.write_validation.get_record_metadata",
            new_callable=AsyncMock,
            return_value=None,
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
            return_value=execute_tool_call_result,
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


class TestInvestigationGateBouncesUnexaminedCreates:
    @pytest.mark.asyncio
    async def test_create_with_no_prior_metadata_call_is_bounced(self):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        create_name = _ext("ns_createRecord")

        tool_call_response = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1", name=create_name, input={"recordType": "customer", "body": {"companyname": "Acme"}}
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        final_response = LLMResponse(
            text_blocks=["Let me check the metadata first."],
            tool_use_blocks=[],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        adapter = _make_adapter([tool_call_response, final_response])

        with _patches():
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert confirmations == [], "an unexamined create must never reach a confirmation card"

        tool_ends = [p for t, p in events if t == "tool_end" and p.get("tool_name") == create_name]
        assert len(tool_ends) == 1
        assert tool_ends[0]["success"] is False
        # Machine-checkable AND human-readable — the summary must name what
        # is being asked for, not a generic failure string.
        assert "metadata" in tool_ends[0]["result_summary"].lower()

        responses_events = [p for t, p in events if t == "response"]
        assert len(responses_events) == 1
        tool_calls_log = responses_events[0].tool_calls_log
        assert len(tool_calls_log) == 1


class TestInvestigationGateOneBouncePerTurnPerRecordType:
    @pytest.mark.asyncio
    async def test_second_proposal_same_turn_passes_through_even_without_metadata_call(self):
        """A stubborn model that never calls ns_getRecordTypeMetadata still
        gets its SECOND proposal through to a card — the gate cannot loop
        forever."""
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        create_name = _ext("ns_createRecord")
        bad_input = {"recordType": "customer", "body": {"companyname": "Acme"}}

        response_1 = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[ToolUseBlock(id="t1", name=create_name, input=dict(bad_input))],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        response_2 = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[ToolUseBlock(id="t2", name=create_name, input=dict(bad_input))],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        final_response = LLMResponse(text_blocks=["done"], tool_use_blocks=[], usage=TokenUsage(10, 10))
        adapter = _make_adapter([response_1, response_2, final_response])

        with _patches():
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert len(confirmations) == 1, "the second attempt must reach a card despite no metadata call ever happening"

    @pytest.mark.asyncio
    async def test_prior_metadata_call_same_record_type_satisfies_gate_immediately(self):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        metadata_name = _ext("ns_getRecordTypeMetadata")
        create_name = _ext("ns_createRecord")

        metadata_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[ToolUseBlock(id="m1", name=metadata_name, input={"recordType": "customer"})],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        create_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1", name=create_name, input={"recordType": "customer", "body": {"companyname": "Acme"}}
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        final_response = LLMResponse(text_blocks=["done"], tool_use_blocks=[], usage=TokenUsage(10, 10))
        adapter = _make_adapter([metadata_call, create_call, final_response])

        with _patches(execute_tool_call_result=json.dumps({"metadata": {"properties": {}}})):
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert len(confirmations) == 1, (
            "a prior metadata call for the SAME record type must satisfy the gate on the FIRST create attempt"
        )

    @pytest.mark.asyncio
    async def test_metadata_call_for_a_different_record_type_does_not_satisfy_gate(self):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        metadata_name = _ext("ns_getRecordTypeMetadata")
        create_name = _ext("ns_createRecord")

        metadata_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[ToolUseBlock(id="m1", name=metadata_name, input={"recordType": "invoice"})],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        create_call = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1", name=create_name, input={"recordType": "customer", "body": {"companyname": "Acme"}}
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        final_response = LLMResponse(text_blocks=["done"], tool_use_blocks=[], usage=TokenUsage(10, 10))
        adapter = _make_adapter([metadata_call, create_call, final_response])

        with _patches(execute_tool_call_result=json.dumps({"metadata": {"properties": {}}})):
            events = await _run(agent, adapter)

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert confirmations == [], (
            "metadata fetched for a DIFFERENT record type must not satisfy this record type's gate"
        )
        tool_ends = [p for t, p in events if t == "tool_end" and p.get("tool_name") == create_name]
        assert tool_ends and tool_ends[0]["success"] is False


class TestInvestigationGateScope:
    @pytest.mark.asyncio
    async def test_update_is_never_gated(self):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        update_name = _ext("ns_updateRecord")

        response = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1",
                    name=update_name,
                    input={"recordType": "customer", "id": "1", "body": {"companyname": "Acme"}},
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        final_response = LLMResponse(text_blocks=["done"], tool_use_blocks=[], usage=TokenUsage(10, 10))
        adapter = _make_adapter([response, final_response])

        with _patches():
            events = await _run(agent, adapter, task="Update the customer's name.")

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert len(confirmations) == 1

    @pytest.mark.asyncio
    async def test_delete_is_never_gated(self):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        delete_name = _ext("ns_deleteRecord")

        response = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[ToolUseBlock(id="t1", name=delete_name, input={"recordType": "customer", "id": "1"})],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        final_response = LLMResponse(text_blocks=["done"], tool_use_blocks=[], usage=TokenUsage(10, 10))
        adapter = _make_adapter([response, final_response])

        with _patches():
            events = await _run(agent, adapter, task="Delete the customer.")

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert len(confirmations) == 1

    @pytest.mark.asyncio
    async def test_upsert_is_gated_like_create(self):
        from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

        agent = _make_agent()
        upsert_name = _ext("ns_upsertRecord")

        response = LLMResponse(
            text_blocks=[],
            tool_use_blocks=[
                ToolUseBlock(
                    id="t1", name=upsert_name, input={"recordType": "customer", "body": {"companyname": "Acme"}}
                )
            ],
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )
        final_response = LLMResponse(text_blocks=["done"], tool_use_blocks=[], usage=TokenUsage(10, 10))
        adapter = _make_adapter([response, final_response])

        with _patches():
            events = await _run(agent, adapter, task="Upsert the customer.")

        confirmations = [p for t, p in events if t == "confirmation_required"]
        assert confirmations == [], "upsert with no prior metadata call must be gated exactly like create"
