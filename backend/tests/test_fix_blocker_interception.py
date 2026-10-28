"""Blocker cards never ride in tool results, so the result interception leaves these tools alone; a card id
is shown once per turn (2026-10-27). How the cards travel: test_fix_blocker_side_channel. Synthetic data only.
"""

import json

import pytest

from app.services.chat.orchestrator import _card_shown, _intercept_tool_result
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


@pytest.mark.parametrize(
    "name",
    [
        "transaction_ops_propose_credit",
        "transaction_ops_propose_credit_reallocation",
        "transaction_ops_accounting_evidence",
    ],
)
def test_the_fix_tools_results_are_not_intercepted(name):
    text = json.dumps({"success": False, "refused": "source_not_final", "blocker": {"who_acts": "Solidus operations"}})
    assert _intercept_tool_result(name, text) == (None, None, text)


def test_a_card_already_shown_this_turn_is_shown_once():
    card = _card()
    assert _card_shown([], card) is False
    assert _card_shown([card], dict(card)) is True
    assert _card_shown([card], {**card, "card_id": "other"}) is False
    assert _card_shown([card], {"title": "no id"}) is False
