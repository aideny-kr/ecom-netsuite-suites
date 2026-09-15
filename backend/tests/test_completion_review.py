import json
from unittest.mock import AsyncMock

import pytest

from app.services.chat.completion_review import AnswerReview, review_answer, review_packet
from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock


def packet(observations=None, card=False):
    return review_packet("Investigate the case", "Here is the result", observations or [], [], None, card)


def adapter_result(**overrides):
    fields = {
        "supported": True,
        "answer_kind": "explanation",
        "claims_new_approval_card": False,
        "evidence_ids": [],
        "feedback": "",
    }
    fields.update(overrides)
    response = LLMResponse(
        tool_use_blocks=[ToolUseBlock("r", "review_answer", fields)], usage=TokenUsage(11, 12, 13, 14)
    )
    adapter = AsyncMock()
    adapter.create_message.return_value = response
    return adapter


async def test_review_uses_same_selected_adapter_without_data_tools_or_global_model():
    adapter = adapter_result()
    result = await review_answer(adapter=adapter, model="tenant-model", packet=packet())
    assert result.review.supported
    call = adapter.create_message.call_args.kwargs
    assert call["model"] == "tenant-model"
    assert [t["name"] for t in call["tools"]] == ["review_answer"]
    assert result.usage == TokenUsage(11, 12, 13, 14)


async def test_server_card_state_overrules_reviewer_claim_of_preparation():
    adapter = adapter_result(claims_new_approval_card=True)
    result = await review_answer(adapter=adapter, model="model", packet=packet())
    assert result.review.supported is False
    assert "No new approval card" in result.review.feedback


async def test_emitted_card_is_not_rejected_by_card_state_check():
    result = await review_answer(
        adapter=adapter_result(claims_new_approval_card=True), model="model", packet=packet(card=True)
    )
    assert result.review.supported


@pytest.mark.parametrize("kind", ["observed_facts", "action_outcome"])
async def test_material_facts_require_actual_receipt_references(kind):
    result = await review_answer(adapter=adapter_result(answer_kind=kind), model="model", packet=packet())
    assert result.review.supported is False


async def test_invented_reference_is_not_accepted_and_consumption_is_counted():
    result = await review_answer(adapter=adapter_result(evidence_ids=["made-up"]), model="model", packet=packet())
    assert result.review is None
    assert result.error == "ValueError"
    assert result.usage.output_tokens == 12


async def test_existing_receipt_does_not_force_another_read():
    result = await review_answer(
        adapter=adapter_result(answer_kind="observed_facts", evidence_ids=["current:0"]),
        model="model",
        packet=packet([{"tool": "arbitrary_plugin", "result": {"total": 100}}]),
    )
    assert result.review.supported


async def test_review_failure_has_no_implicit_success_or_extra_retry():
    adapter = AsyncMock()
    adapter.create_message.side_effect = TimeoutError()
    result = await review_answer(adapter=adapter, model="model", packet=packet())
    assert result.review is None
    assert result.error == "TimeoutError"
    assert adapter.create_message.await_count == 1


def test_verbose_feedback_is_bounded_without_losing_the_decision():
    review = AnswerReview(
        supported=False, answer_kind="limitation", claims_new_approval_card=False, evidence_ids=[], feedback="x" * 3000
    )
    assert review.supported is False
    assert len(review.feedback) == 1200


def test_assistant_narrative_is_not_an_execution_receipt():
    p = review_packet("task", "answer", [], [{"role": "assistant", "content": "I posted it"}], None, False)
    assert p["evidence"] == []
    assert p["conversation"][0]["content"] == "I posted it"


def test_oversized_observation_remains_explicitly_incomplete():
    p = packet([{"tool": "arbitrary_mcp", "result": "x" * 10000}])
    assert p["evidence"][0]["observation"]["truncated"] is True
    assert len(json.dumps(p)) < 4000


@pytest.mark.parametrize(
    "task",
    [
        "Summarize the plan, no data lookup needed.",
        "Explain how sales tax works.",
        "Ask me which BigQuery dataset to use for order counts.",
    ],
)
async def test_enabled_loop_accepts_semantics_without_keyword_forced_queries(monkeypatch, task):
    from app.services.chat.completion_review import ReviewResult
    from tests.test_write_investigation_gate import _make_adapter, _make_agent, _patches, _run

    agent = _make_agent()
    agent._evidence_completion_enabled = True
    review = AnswerReview(
        supported=True, answer_kind="explanation", claims_new_approval_card=False, evidence_ids=[], feedback=""
    )
    checker = AsyncMock(return_value=ReviewResult(review, TokenUsage(3, 4, 5, 6), 1))
    monkeypatch.setattr("app.services.chat.completion_review.review_answer", checker)
    with _patches():
        events = await _run(agent, _make_adapter([LLMResponse(text_blocks=["A useful explanation"])]), task)
    assert checker.await_count == 1
    result = next(payload for kind, payload in events if kind == "response")
    assert result.success
    assert result.tokens_used == TokenUsage(3, 4, 5, 6)
    assert result.tool_calls_log == []
    assert [payload for kind, payload in events if kind == "text"] == ["A useful explanation"]


async def test_rejected_draft_is_never_streamed_as_a_prepared_card(monkeypatch):
    from app.services.chat.completion_review import ReviewResult
    from tests.test_write_investigation_gate import _make_adapter, _make_agent, _patches, _run

    agent = _make_agent()
    agent._evidence_completion_enabled = True
    rejection = AnswerReview(
        supported=False,
        answer_kind="action_outcome",
        claims_new_approval_card=True,
        evidence_ids=[],
        feedback="No approval card exists.",
    )
    monkeypatch.setattr(
        "app.services.chat.completion_review.review_answer",
        AsyncMock(return_value=ReviewResult(rejection, TokenUsage(3, 4), 1)),
    )
    adapter = _make_adapter([])

    async def stream(**kwargs):
        yield "text", "Prepared approval card"
        yield "response", LLMResponse(text_blocks=["Prepared approval card"])

    adapter.stream_message = stream
    with _patches():
        events = await _run(agent, adapter)
    assert [payload for kind, payload in events if kind == "text"] == [
        "The investigation is not yet verified. No approval card exists."
    ]
    result = next(payload for kind, payload in events if kind == "response")
    assert result.success is False
    assert result.tokens_used.input_tokens == 6


async def test_metadata_read_does_not_force_an_unrequested_write(monkeypatch):
    from app.services.chat.completion_review import ReviewResult
    from tests.test_write_investigation_gate import _ext, _make_adapter, _make_agent, _patches, _run

    agent = _make_agent()
    agent._evidence_completion_enabled = True
    review = AnswerReview(
        supported=True,
        answer_kind="explanation",
        claims_new_approval_card=False,
        evidence_ids=["current:0"],
        feedback="",
    )
    checker = AsyncMock(return_value=ReviewResult(review, TokenUsage(), 1))
    monkeypatch.setattr("app.services.chat.completion_review.review_answer", checker)
    adapter = _make_adapter(
        [
            LLMResponse(
                tool_use_blocks=[ToolUseBlock("m", _ext("ns_getRecordTypeMetadata"), {"recordType": "customer"})]
            ),
            LLMResponse(text_blocks=["The metadata describes the customer fields."]),
        ]
    )
    with _patches('{"fields": []}'):
        events = await _run(agent, adapter, "Explain the customer record schema.")
    assert not any(kind == "confirmation_required" for kind, _ in events)
    assert not getattr(agent, "_prose_instead_of_write_bounced", False)
    assert checker.call_args.kwargs["packet"]["evidence"][0]["observation"]["tool"].endswith("ns_getRecordTypeMetadata")


async def test_guard_has_a_fixed_review_budget(monkeypatch):
    from app.services.chat.completion_review import CompletionGuard, ReviewResult

    bad = AnswerReview(
        supported=False,
        answer_kind="limitation",
        claims_new_approval_card=False,
        evidence_ids=[],
        feedback="Evidence missing.",
    )
    checker = AsyncMock(return_value=ReviewResult(bad, TokenUsage(), 1))
    monkeypatch.setattr("app.services.chat.completion_review.review_answer", checker)
    guard = CompletionGuard(enabled=True, task="task")
    for _ in range(4):
        await guard.check(answer="answer", adapter=None, model="model", card_emitted=False)
    assert checker.await_count == 2
    assert guard.supported is False


async def test_review_audit_records_actor_usage_and_digests_without_duplicate_raw_evidence(monkeypatch):
    from app.services.chat.completion_review import CompletionGuard

    audit = AsyncMock()
    monkeypatch.setattr("app.services.audit_service.log_event", audit)
    guard = CompletionGuard(
        enabled=True, task="Explain", audit_context={"db": object(), "tenant_id": "tenant", "actor_id": "actor"}
    )
    guard.observe("plugin.read", {}, {"value": "private evidence"})
    await guard.check(answer="Private answer", adapter=adapter_result(), model="tenant-model", card_emitted=False)
    event = audit.call_args.kwargs
    assert event["actor_id"] == "actor"
    assert event["action"] == "agent.answer_review"
    assert event["payload"]["usage"]["output_tokens"] == 12
    assert event["payload"]["approval_granted"] is False
    assert len(event["payload"]["answer_digest"]) == 64
    assert "private evidence" not in json.dumps(event["payload"])
    assert "Private answer" not in json.dumps(event["payload"])


async def test_numeric_feedback_precedes_semantic_review_and_resolves_values(monkeypatch):
    from unittest.mock import MagicMock

    from app.services.chat.completion_review import ReviewResult
    from tests.test_write_investigation_gate import _make_adapter, _make_agent, _patches, _run

    agent = _make_agent()
    agent._evidence_completion_enabled = True
    numeric = MagicMock()
    numeric.feedback.side_effect = ["Use the existing value reference.", None]
    numeric.resolve.return_value = "The verified total is $100."
    agent._metabase_evidence = numeric
    review = AnswerReview(
        supported=True,
        answer_kind="observed_facts",
        claims_new_approval_card=False,
        evidence_ids=["current:0"],
        feedback="",
    )
    checker = AsyncMock(return_value=ReviewResult(review, TokenUsage(), 1))
    monkeypatch.setattr("app.services.chat.completion_review.review_answer", checker)
    adapter = _make_adapter(
        [LLMResponse(text_blocks=["The total is $101."]), LLMResponse(text_blocks=["The total is {{ref}}."])]
    )
    with _patches():
        events = await _run(agent, adapter, "Explain the total already retrieved")
    assert checker.await_count == 1
    assert checker.call_args.kwargs["packet"]["answer"] == "The verified total is $100."
    assert [v for k, v in events if k == "text"] == ["The verified total is $100."]


async def test_exhausted_numeric_failure_does_not_consume_semantic_review(monkeypatch):
    from unittest.mock import MagicMock

    from app.services.chat.metabase_evidence import UNVERIFIED
    from tests.test_write_investigation_gate import _make_adapter, _make_agent, _patches, _run

    agent = _make_agent()
    agent._evidence_completion_enabled = True
    agent.max_steps = 0
    agent._metabase_evidence = MagicMock()
    agent._metabase_evidence.feedback.return_value = "Unsupported figure"
    checker = AsyncMock()
    monkeypatch.setattr("app.services.chat.completion_review.review_answer", checker)
    with _patches():
        events = await _run(agent, _make_adapter([LLMResponse(text_blocks=["$101"])]))
    checker.assert_not_awaited()
    assert [v for k, v in events if k == "text"] == [UNVERIFIED]
    assert next(v for k, v in events if k == "response").success is False


async def test_truncated_request_cannot_receive_a_full_scope_success():
    p = review_packet("x" * 5000, "Done", [], [], None, False)
    result = await review_answer(adapter=adapter_result(), model="model", packet=p)
    assert not result.review.supported
    assert "scope was not verified" in result.review.feedback


async def test_nonstreaming_path_uses_review_and_preserves_usage(monkeypatch):
    from app.services.chat.completion_review import ReviewResult
    from tests.test_write_investigation_gate import _make_agent, _patches

    agent = _make_agent()
    agent._evidence_completion_enabled = True
    review = AnswerReview(
        supported=True, answer_kind="explanation", claims_new_approval_card=False, evidence_ids=[], feedback=""
    )
    checker = AsyncMock(return_value=ReviewResult(review, TokenUsage(1, 2, 3, 4), 1))
    monkeypatch.setattr("app.services.chat.completion_review.review_answer", checker)
    adapter = AsyncMock()
    adapter.create_message.return_value = LLMResponse(text_blocks=["Explanation"], usage=TokenUsage(5, 6, 7, 8))
    with _patches():
        result = await agent.run("Summarize the plan", {}, AsyncMock(), adapter, "model")
    assert result.success and result.data == "Explanation"
    assert result.tokens_used == TokenUsage(6, 8, 10, 12)
    assert adapter.create_message.await_count == 1


async def test_real_generic_card_remains_a_proposal_and_is_observed_by_review(monkeypatch):
    from app.services.chat.completion_review import ReviewResult
    from tests.test_write_investigation_gate import _call, _done, _make_adapter, _make_agent, _patches, _run

    agent = _make_agent()
    agent._evidence_completion_enabled = True
    review = AnswerReview(
        supported=True,
        answer_kind="action_outcome",
        claims_new_approval_card=True,
        evidence_ids=["current:0"],
        feedback="",
    )
    checker = AsyncMock(return_value=ReviewResult(review, TokenUsage(), 1))
    monkeypatch.setattr("app.services.chat.completion_review.review_answer", checker)
    with _patches():
        events = await _run(agent, _make_adapter([_call(), _done()]))
    assert len([p for k, p in events if k == "confirmation_required"]) == 1
    packet = checker.call_args.kwargs["packet"]
    assert packet["server_state"]["new_approval_card_emitted"] is True
    assert packet["evidence"]
    assert next(p for k, p in events if k == "response").success


async def test_enumerated_unsupported_claim_overrules_a_supported_boolean():
    result = await review_answer(
        adapter=adapter_result(unsupported_claims=["Calls an observed change a proven price-match cause"]),
        model="model",
        packet=packet(),
    )
    assert not result.review.supported
    assert "price-match cause" in result.review.feedback


async def test_exhausted_review_output_cannot_certify_an_answer():
    adapter = adapter_result()
    adapter.create_message.return_value.usage.output_tokens = 1024
    result = await review_answer(adapter=adapter, model="model", packet=packet())
    assert not result.review.supported
    assert "incomplete" in result.review.feedback


async def test_verbose_unsupported_claim_retains_actionable_rejection():
    result = await review_answer(
        adapter=adapter_result(unsupported_claims=["Unsupported cause " + "x" * 1000]), model="model", packet=packet()
    )
    assert result.error is None and not result.review.supported
    assert len(result.review.unsupported_claims[0]) == 180
    assert "Unsupported cause" in result.review.feedback
