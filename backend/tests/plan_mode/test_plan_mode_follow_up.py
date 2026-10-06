"""Plan Mode does not force a clarification card once the conversation has settled a source.

2026-10-06, Framework: after the user picked NetSuite for a Yucca question, "break down country
and total revenue" and then "... from sales order" each got a forced clarify card (the regex
matches "revenue"). Over 30 days, 5 of Framework's 9 forced cards came after a source was
already chosen in the same chat. The histories below are the real message shapes from that chat.
"""

import inspect

from app.services.chat.plan_mode.ambiguity_signal import build_augmentation_prompt, plan_mode_should_fire


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


def fires(query, history, **kw):
    return plan_mode_should_fire(
        query, plan_mode_enabled=kw.get("enabled", True), resume_active=kw.get("resume", False), history=history
    )


def test_a_first_revenue_question_still_gets_the_card():
    assert fires("what was revenue last quarter?", [_user("what was revenue last quarter?")])


def test_a_follow_up_after_an_answer_on_a_chosen_source_does_not():
    history = [_user("since yucca launched ..."), SOURCE_QUESTION, _user("NetSuite"), ANSWER_ON_NETSUITE]
    assert not fires("break down country and total revenue", history + [_user("break down country and total revenue")])


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
    assert not fires(query, history + [_user(query)])


def test_an_unanswered_source_question_does_not_count_as_a_choice():
    history = [_user("hello"), SOURCE_QUESTION]
    assert fires("and the revenue?", history + [_user("and the revenue?")])


def test_the_flag_resume_and_wording_gates_still_apply():
    assert not fires("revenue last quarter", [], enabled=False)
    assert not fires("revenue last quarter", [], resume=True)
    assert not fires("how many orders yesterday", [])


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
    assert source.count("plan_mode_should_fire(") == 1
    assert "is_financial_ambiguous(sanitized_input)" not in source
    assert source.count("_plan_mode_fires") >= 3  # initialised, decided, read by both sites
