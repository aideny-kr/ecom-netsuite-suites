"""A blocked fix reaches the screen as a card with its records; the model reads who acts, no amounts.

2026-10-27: refused fixes and the evidence tool carry the blocker's card beside an amount-free summary.
The interceptor shows the card and hands the model the summary only. Synthetic data only.
"""

import json

import pytest

from app.services.chat.orchestrator import _intercept_tool_result
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


def _blocked():
    blocker = fix_blockers.blocker("source_not_final", fix_blockers.source_finality(SOURCE))
    card = fix_blockers.card(blocker, order_reference="R123456789", currency="USD")
    return blocker, card


@pytest.mark.parametrize(
    "name",
    [
        "transaction_ops.propose_credit",
        "transaction_ops_propose_credit",
        "transaction_ops.propose_credit_reallocation",
        "transaction_ops_propose_credit_reallocation",
    ],
)
def test_a_refused_fix_shows_its_blocker_card_and_the_model_gets_no_amounts(name):
    blocker, card = _blocked()
    result = {
        "success": False,
        "refused": "source_not_final",
        "detail": fix_blockers.model_detail("source_not_final", fix_blockers.source_finality(SOURCE)),
        "guidance": "…",
        "blocker": fix_blockers.for_model(blocker, card_shown=True),
        "blocker_card": card,
        "financial_writes": 0,
    }
    event_type, event, for_model = _intercept_tool_result(name, json.dumps(result))
    assert event_type == "result_card" and event == card
    seen = json.loads(for_model)
    assert seen["refused"] == "source_not_final" and seen["blocker"]["who_acts"] == "Solidus operations"
    assert "blocker_card" not in seen and seen["detail"]["checks"] == ["open_adjustments"]
    assert "47.04" not in for_model and "Manual Tax Correction" not in for_model


@pytest.mark.parametrize("name", ["transaction_ops.accounting_evidence", "transaction_ops_accounting_evidence"])
def test_the_evidence_view_shows_the_card_and_keeps_the_rest_for_the_model(name):
    blocker, card = _blocked()
    view = {
        "case_id": "c1",
        "evidence_summary": {"candidate_available": False},
        "fix_blocker": fix_blockers.for_model(blocker, card_shown=True),
        "fix_blocker_card": card,
        "success": True,
    }
    event_type, event, for_model = _intercept_tool_result(name, json.dumps(view))
    assert event_type == "result_card" and event == card
    seen = json.loads(for_model)
    assert seen["evidence_summary"] == {"candidate_available": False} and seen["fix_blocker"]["open_records"] == 1
    assert "fix_blocker_card" not in seen and "47.04" not in for_model


@pytest.mark.parametrize(
    "name, result",
    [
        (
            "transaction_ops_propose_credit",
            {"success": False, "refused": "outcome_does_not_match_source", "detail": {"required": {"tax": "450.20"}}},
        ),
        ("transaction_ops_accounting_evidence", {"success": True, "case_id": "c1"}),
        ("transaction_ops_propose_credit", {"success": False, "blocker_card": {"title": "no card id"}}),
    ],
)
def test_without_a_valid_card_the_result_is_untouched(name, result):
    text = json.dumps(result)
    assert _intercept_tool_result(name, text) == (None, None, text)


def test_a_result_that_is_not_json_is_untouched():
    assert _intercept_tool_result("transaction_ops_propose_credit", "oops") == (None, None, "oops")


def test_a_card_already_shown_this_turn_is_shown_once():
    from app.services.chat.orchestrator import _card_shown

    _, card = _blocked()
    assert _card_shown([], card) is False
    assert _card_shown([card], dict(card)) is True
    assert _card_shown([card], {**card, "card_id": "other"}) is False
    assert _card_shown([card], {"title": "no id"}) is False


def test_an_unprojected_evidence_response_still_shows_the_card_and_hides_it_from_the_model():
    # Review F1: the legacy loop, or a view too large to project, hands the interceptor the full response.
    blocker, card = _blocked()
    full = {
        "success": True,
        "case_id": "c1",
        "accounting_evidence": {"completed_at": "2026-10-27T19:47:00Z"},
        "fix_blocker_card": card,
        "model_context": {
            "version": 1,
            "data": {
                "case_id": "c1",
                "fix_blocker": fix_blockers.for_model(blocker, card_shown=True),
                "fix_blocker_card": card,
            },
        },
    }
    event_type, event, for_model = _intercept_tool_result("transaction_ops_accounting_evidence", json.dumps(full))
    assert event_type == "result_card" and event == card
    assert "fix_blocker_card" not in for_model and "47.04" not in for_model
    assert json.loads(for_model)["model_context"]["data"]["fix_blocker"]["open_records"] == 1
