"""Plan Mode does not force a clarification card once the conversation has settled a source.

2026-10-06, Framework: after the user picked NetSuite for a Yucca question, "break down country
and total revenue" and then "... from sales order" each got a forced clarify card (the regex
matches "revenue"). Over 30 days, 5 of Framework's 9 forced cards came after a source was
already chosen in the same chat. The histories below are the real message shapes from that chat.
"""

import inspect
import uuid

from app.services.chat.agents.unified_agent import UnifiedAgent
from app.services.chat.plan_mode.ambiguity_signal import build_augmentation_prompt, plan_mode_decision


def _ctx(sources, pending=False):
    return {
        "request_context": {
            "version": 1,
            "kind": "analytics",
            "sources": sources,
            "excluded_sources": [],
            "pending_source": pending,
        }
    }


SOURCE_QUESTION = {
    "role": "assistant",
    "content": "Which data source should I use?",
    "structured_output": _ctx([], pending=True),
}
ANSWER_ON_NETSUITE = {
    "role": "assistant",
    "content": "Yucca orders began on 09/30/2026 ...",
    "structured_output": {"type": "data_table", **_ctx(["netsuite"])},
}
CHOSEN_CARD = {
    "role": "assistant",
    "content": "",
    "structured_output": {
        "type": "clarification",
        "status": "chosen",
        "chosen_id": "A",
        "default_id": "A",
        "options": [
            {"id": "A", "title": "NetSuite GL recognized revenue", "source": "netsuite", "is_default": True},
            {"id": "B", "title": "NetSuite booked sales orders", "source": "netsuite", "is_default": False},
            {"id": "C", "title": "BigQuery checkout totals", "source": "bigquery", "is_default": False},
        ],
        **_ctx([], pending=True),
    },
}
CANCELLED = {"role": "assistant", "content": "*(Response cancelled)*", "structured_output": {"type": "data_table"}}


def _user(text):
    return {"role": "user", "content": text, "structured_output": None}


def decide(query, history, **kw):
    return plan_mode_decision(
        query, plan_mode_enabled=kw.get("enabled", True), resume_active=kw.get("resume", False), history=history
    )


def test_a_first_revenue_question_still_gets_the_card():
    assert decide("what was revenue last quarter?", [_user("what was revenue last quarter?")]) == "force"


def test_a_follow_up_after_an_answer_on_a_chosen_source_does_not():
    history = [_user("since yucca launched ..."), SOURCE_QUESTION, _user("NetSuite"), ANSWER_ON_NETSUITE]
    # Not forced, but the model may still ask (#394 review R1: offering keeps the fallback).
    assert (
        decide("break down country and total revenue", history + [_user("break down country and total revenue")])
        == "offer"
    )


def test_a_follow_up_after_a_chosen_card_and_a_cancel_does_not():
    history = [
        _user("since yucca launched ..."),
        SOURCE_QUESTION,
        _user("NetSuite"),
        ANSWER_ON_NETSUITE,
        _user("break down country and total revenue"),
        CHOSEN_CARD,
        CANCELLED,
    ]
    query = "break down country and total revenue from sales order"
    assert decide(query, history + [_user(query)]) == "offer"


def test_an_unanswered_source_question_does_not_count_as_a_choice():
    history = [_user("hello"), SOURCE_QUESTION]
    assert decide("and the revenue?", history + [_user("and the revenue?")]) == "force"


def test_the_flag_resume_and_wording_gates_still_apply():
    assert decide("revenue last quarter", [], enabled=False) == "off"
    assert decide("revenue last quarter", [], resume=True) == "off"
    # A turn the regex does not match is untouched, whatever the history.
    assert decide("how many orders yesterday", []) == "off"
    assert decide("how many orders yesterday", [ANSWER_ON_NETSUITE]) == "off"


def test_the_default_follows_the_basis_the_user_or_conversation_names():
    prompt = build_augmentation_prompt(connected_sources=["netsuite", "bigquery"])
    assert "already names a basis" in prompt
    assert "booked sales orders" in prompt
    # GL is the fallback, not an unconditional default for every "revenue".
    assert 'Default preferences: NetSuite GL for "revenue"' not in prompt


def test_the_orchestrator_decides_once_and_both_sites_use_it():
    """The augmentation and the forced tool choice must not disagree, so the regex is not
    consulted at either site; both read one precomputed decision."""
    from app.services.chat import orchestrator

    source = inspect.getsource(orchestrator.run_chat_turn)
    assert source.count("plan_mode_decision(") == 1
    assert "is_financial_ambiguous(sanitized_input)" not in source
    # The augmentation and the forced tool choice both read the decision.
    assert 'if _plan_mode_decision == "force"\n                            else None' in source
    assert 'if not _is_chitchat and _plan_mode_decision == "force":' in source
    assert 'plan_mode_offer_clarify=_plan_mode_decision == "offer"' in source


def _agent_with_tools():
    agent = UnifiedAgent(tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), correlation_id="t")
    agent._tool_defs = [{"name": "netsuite_suiteql"}, {"name": "present_result"}]
    return agent


def test_an_offer_turn_gives_the_model_clarify_alongside_its_tools():
    """The agent rebuilds its tools without the Plan Mode flag, so clarify must be added back."""
    agent = _agent_with_tools()
    agent._apply_plan_mode_tools(clarify_only=False, offer_clarify=True, resume_source=None)
    names = [t["name"] for t in agent._tool_defs]
    assert names[:2] == ["netsuite_suiteql", "present_result"] and names.count("clarify") == 1
    agent._apply_plan_mode_tools(clarify_only=False, offer_clarify=True, resume_source=None)
    assert [t["name"] for t in agent._tool_defs].count("clarify") == 1  # never twice


def test_a_forced_turn_keeps_only_clarify_and_an_off_turn_adds_nothing():
    agent = _agent_with_tools()
    agent._apply_plan_mode_tools(clarify_only=True, offer_clarify=False, resume_source=None)
    assert [t["name"] for t in agent._tool_defs] == ["clarify"]
    agent = _agent_with_tools()
    agent._apply_plan_mode_tools(clarify_only=False, offer_clarify=False, resume_source=None)
    assert [t["name"] for t in agent._tool_defs] == ["netsuite_suiteql", "present_result"]


def test_both_agent_entry_points_use_the_one_helper():
    run_src = inspect.getsource(UnifiedAgent.run)
    stream_src = inspect.getsource(UnifiedAgent.run_streaming)
    for src in (run_src, stream_src):
        assert "self._apply_plan_mode_tools(" in src
        assert "CLARIFY_TOOL_SCHEMA" not in src  # the schema is handled in the helper only
