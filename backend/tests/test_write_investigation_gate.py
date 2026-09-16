"""Automatic schema investigation through the real generic write loop.

Validation obtains current/cached schema; invalid proposals remain blocked
and every mutation needs HITL without prescribing model tool-call order.
"""

from __future__ import annotations

import contextlib
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
    """Unknown schema retains existing explicitly unvalidated review behavior.

    Individual tests replace metadata with verified fields to exercise repair.
    """
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


def _assert_no_mutations(execute):
    from app.services.chat.mutation_guard import classify_mutation

    assert all(classify_mutation(call.kwargs["tool_name"]) is None for call in execute.await_args_list)


def _call(tool="ns_createRecord", *, fields=None, call_id="create"):
    from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock

    return LLMResponse(
        text_blocks=[],
        tool_use_blocks=[
            ToolUseBlock(
                id=call_id,
                name=_ext(tool),
                input={"recordType": "customer", "body": fields or {"companyname": "Acme"}},
            )
        ],
        usage=TokenUsage(10, 10),
    )


def _done():
    from app.services.chat.llm_adapter import LLMResponse, TokenUsage

    return LLMResponse(text_blocks=["Proposal ready for review."], tool_use_blocks=[], usage=TokenUsage(10, 10))


def _metadata():
    from app.services.chat.record_metadata_service import FieldSpec, RecordMetadata

    return RecordMetadata(
        record_type="customer",
        fields=[
            FieldSpec(name="companyname", label="Name", required=True),
            FieldSpec(name="subsidiary", label="Subsidiary", required=True),
        ],
    )


class TestAutomaticSchemaInvestigation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", ["ns_createRecord", "ns_upsertRecord"])
    async def test_valid_proposal_obtains_schema_without_a_model_metadata_hop(self, tool):
        agent = _make_agent()
        adapter = _make_adapter([_call(tool, fields={"companyname": "Acme", "subsidiary": {"id": "1"}}), _done()])
        with (
            _patches(),
            patch(
                "app.services.chat.write_validation.get_record_metadata", AsyncMock(return_value=_metadata())
            ) as metadata,
            patch("app.services.chat.tools.execute_tool_call", AsyncMock()) as execute,
        ):
            events = await _run(agent, adapter)
        assert len([p for t, p in events if t == "confirmation_required"]) == 1
        metadata.assert_awaited_once()
        assert metadata.call_args.kwargs["record_type"] == "customer"
        assert metadata.call_args.kwargs["mutation_tool_name"] == _ext(tool)
        assert metadata.call_args.kwargs["tenant_id"] == agent.tenant_id
        _assert_no_mutations(execute)
        assert not any(p.get("success") is False for t, p in events if t == "tool_end")

    @pytest.mark.asyncio
    async def test_missing_fields_receive_validator_feedback_before_any_card(self):
        agent = _make_agent()
        adapter = _make_adapter(
            [
                _call(call_id="missing"),
                _call(fields={"companyname": "Acme", "subsidiary": {"id": "1"}}, call_id="repaired"),
                _done(),
            ]
        )
        with (
            _patches(),
            patch("app.services.chat.write_validation.get_record_metadata", AsyncMock(return_value=_metadata())),
            patch("app.services.chat.tools.execute_tool_call", AsyncMock()) as execute,
        ):
            events = await _run(agent, adapter)
        failures = [i for i, (t, p) in enumerate(events) if t == "tool_end" and p.get("success") is False]
        cards = [i for i, (t, _) in enumerate(events) if t == "confirmation_required"]
        assert len(cards) == 1 and failures and failures[0] < cards[0]
        _assert_no_mutations(execute)

    @pytest.mark.asyncio
    async def test_unresolved_required_fields_do_not_become_a_card(self):
        agent = _make_agent()
        adapter = _make_adapter([_call(), _done()])
        with (
            _patches(),
            patch("app.services.chat.write_validation.get_record_metadata", AsyncMock(return_value=_metadata())),
            patch("app.services.chat.tools.execute_tool_call", AsyncMock()) as execute,
        ):
            events = await _run(agent, adapter)
        assert not [p for t, p in events if t == "confirmation_required"]
        _assert_no_mutations(execute)

    @pytest.mark.asyncio
    async def test_unknown_schema_preserves_explicit_unvalidated_review(self):
        agent = _make_agent()
        adapter = _make_adapter([_call(), _done()])
        with _patches(), patch("app.services.chat.tools.execute_tool_call", AsyncMock()) as execute:
            events = await _run(agent, adapter)
        assert len([p for t, p in events if t == "confirmation_required"]) == 1
        assert agent._last_validation.unvalidated is True
        _assert_no_mutations(execute)

    @pytest.mark.asyncio
    async def test_identical_proposals_show_one_card(self):
        agent = _make_agent()
        adapter = _make_adapter([_call(call_id="first"), _call(call_id="repeated"), _done()])
        with _patches(), patch("app.services.chat.tools.execute_tool_call", AsyncMock()) as execute:
            events = await _run(agent, adapter)
        assert len([p for t, p in events if t == "confirmation_required"]) == 1
        _assert_no_mutations(execute)

    @pytest.mark.asyncio
    async def test_transaction_workflow_still_requires_its_exact_accounting_proposal(self):
        agent = _make_agent()
        agent._transaction_workflow = True
        adapter = _make_adapter([_call(), _done()])
        with _patches(), patch("app.services.chat.tools.execute_tool_call", AsyncMock()) as execute:
            events = await _run(agent, adapter)
        assert not [p for t, p in events if t == "confirmation_required"]
        _assert_no_mutations(execute)


@pytest.fixture(autouse=True)
def _netsuite_classification_boundary(monkeypatch):
    """These write-flow units use NetSuite; connector identity is tested separately."""
    from app.services.chat.mutation_guard import classify_mutation

    monkeypatch.setattr(
        "app.services.chat.mutation_guard.classify_connector_mutation",
        AsyncMock(side_effect=lambda tool_name, *_: classify_mutation(tool_name)),
    )
