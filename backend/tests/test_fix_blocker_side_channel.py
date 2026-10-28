"""Blocker cards travel beside the tool result, never in it (2026-10-27, review rounds 1-4).

Four review rounds each found the card's records reaching the model by another route: an unprojected
read, a missing card, the projection's size fallback, a runner with no interceptor. So the tools never put
the card in their result. They queue it on the session; the agent loop shows it after the tool call, and
whatever reads the result (model, interceptor, runner) never has it. Synthetic data only.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.transaction_ops import fix_blockers

SOURCE = {
    "state": "complete",
    "requires_review": False,
    "completed_at": "2026-04-21T20:48:00Z",
    "total": "2356.04",
    "tax_total": "111.08",
    "adjustments": [
        {
            "id": 11,
            "label": "Manual Tax Correction",
            "amount": "-47.04",
            "finalized": False,
            "adjustable_type": "Spree::Order",
            "created_at": "2026-10-21T15:03:00Z",
        }
    ],
}


def _card():
    value = fix_blockers.blocker("source_not_final", fix_blockers.source_finality(SOURCE))
    return fix_blockers.card(value, order_reference="R123456789", currency="USD")


def test_cards_are_queued_on_the_session_and_taken_once():
    db = SimpleNamespace(info={})
    card = _card()
    fix_blockers.queue_card(db, card)
    fix_blockers.queue_card(db, None)  # nothing to show adds nothing
    assert fix_blockers.take_cards(db) == [card]
    assert fix_blockers.take_cards(db) == []
    assert fix_blockers.take_cards(SimpleNamespace()) == []  # a session without info is not an error


def test_the_summary_joins_the_model_view_only_when_it_fits():
    summary = {"blocked": "source_not_final", "who_acts": "Solidus operations"}
    small = {"case_id": "c1"}
    assert fix_blockers.with_summary(small, summary) == {"case_id": "c1", "fix_blocker": summary}
    large = {"case_id": "c1", "notes": "x" * fix_blockers.MODEL_VIEW_BUDGET}
    # Already at the budget: the view stays exactly as it was, so the summary cannot tip it into the
    # projection's full-response fallback (review R4-F1).
    assert fix_blockers.with_summary(large, summary) is large


@pytest.mark.parametrize("streaming", [True, False])
async def test_the_agent_shows_a_queued_card_after_the_tool_and_the_model_never_reads_it(db, streaming):
    from app.services.chat.agents.base_agent import BaseSpecialistAgent
    from app.services.chat.llm_adapter import LLMResponse, ToolUseBlock
    from tests.test_metabase_skills import _agent

    agent = _agent([])
    agent._tool_defs.append(
        {"name": "transaction_ops_accounting_evidence", "description": "Evidence", "input_schema": {"type": "object"}}
    )
    card, outputs, step = _card(), [], 0

    async def response(**kwargs):
        nonlocal step
        step += 1
        if step == 1:
            return LLMResponse(
                tool_use_blocks=[ToolUseBlock("ev", "transaction_ops_accounting_evidence", {"case_id": "c1"})]
            )
        return LLMResponse(text_blocks=["Solidus operations must void the open adjustment."])

    async def stream(**kwargs):
        yield "response", await response(**kwargs)

    async def dispatch(tool_name, tool_input, **kwargs):
        fix_blockers.queue_card(db, card)  # as the evidence tool does; its result carries no card
        return json.dumps({"success": True, "case_id": "c1", "fix_blocker": {"who_acts": "Solidus operations"}})

    def build_tool_result(content):
        outputs.extend(part["content"] for part in content if part.get("type") == "tool_result")
        return {"role": "user", "content": content}

    adapter = MagicMock()
    adapter.create_message = response
    adapter.stream_message = stream
    adapter.build_assistant_message.side_effect = lambda r: {"role": "assistant", "content": ""}
    adapter.build_tool_result_message.side_effect = build_tool_result
    with (
        patch("app.services.chat.mutation_guard.classify_connector_mutation", new=AsyncMock(return_value=None)),
        patch("app.services.chat.tools.execute_tool_call", side_effect=dispatch),
        patch(
            "app.services.chat.agents.base_agent.extract_structured_confidence",
            new=AsyncMock(return_value=SimpleNamespace(score=5, source="test")),
        ),
        patch("app.services.chat.agents.base_agent._maybe_store_query_pattern", new=AsyncMock()),
    ):
        if streaming:
            events = [
                e async for e in BaseSpecialistAgent.run_streaming(agent, "Fix R123456789", {}, db, adapter, "test")
            ]
            shown = [value for kind, value in events if kind == "tool_intercept"]
            assert shown == [("result_card", card)]
        else:
            await BaseSpecialistAgent.run(agent, "Fix R123456789", {}, db, adapter, "test")
    assert outputs and all("Manual Tax Correction" not in o and "47.04" not in o for o in outputs)
    assert fix_blockers.take_cards(db) == []  # nothing left over for a later turn


def test_a_groups_blocked_orders_become_one_card():
    # Review round 5 (F1): group preparation reads each order on its own session; its cards are merged.
    one = fix_blockers.card(
        fix_blockers.blocker("source_not_final", fix_blockers.source_finality(SOURCE)),
        order_reference="R111111111",
        currency="USD",
    )
    two = {
        **one,
        "card_id": "fix-blocker:R222222222:source_not_final",
        "rows": [["12", "Price match", "line", "-5.00", None]],
    }
    merged = fix_blockers.group_card([one, two])
    assert merged["title"] == "Fix blocked for 2 orders: Solidus can still change them"
    assert [c["key"] for c in merged["columns"]][:2] == ["order", "id"]
    assert merged["rows"] == [
        ["R111111111", "11", "Manual Tax Correction", "order", "-47.04", "2026-10-21"],
        ["R222222222", "12", "Price match", "line", "-5.00", None],
    ]
    assert fix_blockers.group_card([one]) is one and fix_blockers.group_card([]) is None
