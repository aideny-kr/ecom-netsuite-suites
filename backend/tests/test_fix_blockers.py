"""A refused fix names its blocker, who acts and the exact records (2026-10-27).

Of 19 fresh per-order agent runs on 27 Oct, 18 ended with "I can't prepare a fix" and 17 of those were the
source_not_final guard: Solidus still had an unfinalized adjustment. The guard is right; the agent never
said which adjustment. These tests pin the deterministic blocker that replaces that guesswork.
Synthetic data only.
"""

import json

import pytest

from app.services.transaction_ops import fix_blockers


def _source(**updates):
    return {
        "number": "R123456789",
        "currency": "USD",
        "state": "complete",
        "requires_review": False,
        "completed_at": "2026-04-21T20:48:00Z",
        "payment_state": "paid",
        "total": "2356.04",
        "tax_total": "111.08",
        "payment_total": "2356.04",
        "adjustments": [
            {
                "id": 11,
                "label": "Manual Tax Correction",
                "amount": "-47.04",
                "finalized": False,
                "adjustable_type": "Spree::Order",
                "source_type": None,
                "created_at": "2026-10-21T15:03:00Z",
            },
            {
                "id": 12,
                "label": "Promotion",
                "amount": "-10.00",
                "finalized": True,
                "adjustable_type": "Spree::Order",
                "source_type": "Spree::PromotionAction",
            },
        ],
        "line_items": [
            {
                "adjustments": [
                    # Solidus's own tax-rate adjustments stay open on completed orders by design.
                    {
                        "id": 21,
                        "label": "Tax",
                        "amount": "0.00",
                        "finalized": False,
                        "source_type": "Spree::TaxRate",
                        "adjustable_type": "Spree::LineItem",
                    },
                    {
                        "id": 22,
                        "label": "Price match",
                        "amount": "-5.00",
                        "finalized": False,
                        "source_type": "Spree::PromotionAction",
                        "adjustable_type": "Spree::LineItem",
                        "created_at": "2026-10-22T09:00:00Z",
                    },
                ]
            },
        ],
        "shipments": [
            {
                "adjustments": [
                    {
                        "id": 31,
                        "label": "Shipping Tax",
                        "amount": "0.00",
                        "finalized": False,
                        "source_type": "Spree::TaxRate",
                        "adjustable_type": "Spree::Shipment",
                    }
                ]
            }
        ],
        **updates,
    }


def test_the_open_adjustments_are_named_and_solidus_tax_rates_are_not():
    finality = fix_blockers.source_finality(_source())
    assert [a["id"] for a in finality["open_adjustments"]] == ["11", "22"]
    assert finality["open_adjustments"][0] == {
        "id": "11",
        "label": "Manual Tax Correction",
        "amount": "-47.04",
        "applies_to": "order",
        "created_at": "2026-10-21T15:03:00Z",
    }
    assert finality["open_adjustments"][1]["applies_to"] == "line"
    assert finality["checks"] == ["open_adjustments"]


@pytest.mark.parametrize(
    "updates, check",
    [
        ({"state": "returned"}, "order_not_complete"),
        ({"requires_review": True}, "flagged_for_review"),
        ({"completed_at": None}, "not_completed"),
    ],
)
def test_every_failed_finality_check_is_named(updates, check):
    finality = fix_blockers.source_finality(_source(adjustments=[], line_items=[], **updates))
    assert finality["checks"] == [check] and finality["open_adjustments"] == []


def test_payment_is_checked_only_where_the_guard_requires_it():
    source = _source(adjustments=[], line_items=[], payment_state="balance_due")
    assert fix_blockers.source_finality(source)["checks"] == []
    assert fix_blockers.source_finality(source, require_paid=True)["checks"] == ["payment_not_settled"]


def test_the_list_is_bounded():
    many = [{"id": i, "label": "x", "amount": "-1.00", "finalized": False} for i in range(40)]
    finality = fix_blockers.source_finality(_source(adjustments=many, line_items=[]))
    assert len(finality["open_adjustments"]) == fix_blockers.MAX_OBJECTS
    assert finality["open_adjustments_not_listed"] == 40 - fix_blockers.MAX_OBJECTS


def test_source_not_final_names_solidus_operations_and_the_records():
    blocker = fix_blockers.blocker("source_not_final", fix_blockers.source_finality(_source()))
    assert blocker["owner"] == "Solidus operations"
    assert "void" in blocker["action"].lower() and "finalize" in blocker["action"].lower()
    assert [o["id"] for o in blocker["objects"]] == ["11", "22"]


def test_every_known_refusal_has_an_owner_and_an_action():
    for code in fix_blockers.KNOWN:
        blocker = fix_blockers.blocker(code, {})
        assert blocker["owner"] and blocker["action"] and blocker["title"]


def test_an_unknown_refusal_has_no_invented_blocker():
    assert fix_blockers.blocker("outcome_does_not_match_source", {"required": "1.00"}) is None


def test_the_card_carries_every_record_and_amount():
    blocker = fix_blockers.blocker("source_not_final", fix_blockers.source_finality(_source()))
    card = fix_blockers.card(blocker, order_reference="R123456789", currency="USD", observed_at="2026-10-27T19:47:00Z")
    assert card["kind"] == "table" and card["title"] == blocker["title"]
    assert [c["key"] for c in card["columns"]] == ["id", "label", "applies_to", "amount", "created"]
    assert card["columns"][3] == {
        "key": "amount",
        "label": "Amount",
        "format": "currency",
        "currency": "USD",
        "align": "right",
    }
    assert card["rows"][0] == ["11", "Manual Tax Correction", "order", "-47.04", "2026-10-21"]
    assert card["check"] == {"status": "warn", "text": blocker["action"]}
    assert "R123456789" in card["subtitle"] and "Solidus operations" in card["subtitle"]
    for key in ("card_id", "result_ids", "queries", "tiles", "top_n", "collapsed"):
        assert key in card


def test_a_blocker_without_records_has_no_card():
    assert fix_blockers.card(fix_blockers.blocker("period_locked", {}), order_reference="R1", currency="USD") is None


def test_the_model_learns_who_acts_but_never_the_amounts():
    blocker = fix_blockers.blocker("source_not_final", fix_blockers.source_finality(_source()))
    summary = json.dumps(fix_blockers.for_model(blocker, card_shown=True))
    assert "Solidus operations" in summary and '"open_records": 2' in summary
    assert "47.04" not in summary and "Manual Tax Correction" not in summary


def test_the_evidence_view_names_the_blocker_when_no_fix_is_ready():
    fields = fix_blockers.evidence_fields(
        {"source_refresh": _source()},
        None,
        order_reference="R123456789",
        currency="USD",
        observed_at="2026-10-27T19:47:00Z",
    )
    assert fields["model"]["who_acts"] == "Solidus operations" and fields["model"]["card_shown"]
    assert [row[0] for row in fields["card"]["rows"]] == ["11", "22"]


def test_the_model_part_stays_small_however_long_the_records_are():
    # Review R3-F1: 20 open adjustments with long labels must not grow what the model reads.
    many = [
        {"id": i, "label": "x" * 1200, "amount": "-1.00", "finalized": False, "adjustable_type": "Spree::Order"}
        for i in range(25)
    ]
    fields = fix_blockers.evidence_fields(
        {"source_refresh": _source(adjustments=many, line_items=[])}, None, order_reference="R1", currency="USD"
    )
    assert len(json.dumps(fields["model"])) < 2000 and "xxxx" not in json.dumps(fields["model"])
    assert fields["model"]["open_records"] == 25
    assert all(len(row[1]) <= fix_blockers.MAX_LABEL for row in fields["card"]["rows"])


@pytest.mark.parametrize(
    "evidence, correction",
    [
        ({"source_refresh": _source()}, {"kind": "credit_line_reallocation"}),  # a fix is ready: nothing blocks it
        ({"source_refresh": _source(adjustments=[], line_items=[])}, None),  # nothing open in Solidus
        ({"source_refresh": _source(adjustments=[], line_items=[], state="returned")}, None),  # no records to name
        ({}, None),  # Solidus was not read
    ],
)
def test_the_evidence_view_is_unchanged_without_an_open_adjustment_to_name(evidence, correction):
    assert fix_blockers.evidence_fields(evidence, correction, order_reference="R1", currency="USD") == {}


def test_the_model_never_gets_the_records_even_without_a_card():
    # Review F2: what the model reads is amount-free whether or not the card could be built.
    detail = fix_blockers.model_detail("source_not_final", fix_blockers.source_finality(_source()))
    assert detail == {"checks": ["open_adjustments"], "open_adjustments": 2}
    text = json.dumps(detail)
    assert "47.04" not in text and "Manual Tax Correction" not in text
    other = {"required": {"tax": "450.20"}}
    assert fix_blockers.model_detail("outcome_does_not_match_source", other) is other


def test_other_failed_checks_are_named_beside_the_open_adjustments():
    # Review F3: a review flag blocks the next proposal too, so it is said, not dropped.
    finality = fix_blockers.source_finality(_source(requires_review=True))
    blocker = fix_blockers.blocker("source_not_final", finality)
    assert blocker["also_blocking"] == ["Solidus flags the order for review"]
    shown = fix_blockers.card(blocker, order_reference="R1", currency="USD")
    assert "Solidus flags the order for review" in shown["check"]["text"]
    assert fix_blockers.for_model(blocker, card_shown=True)["also_blocking"] == ["Solidus flags the order for review"]


def test_with_nothing_open_the_action_names_what_solidus_must_settle():
    finality = fix_blockers.source_finality(_source(adjustments=[], line_items=[], state="returned"))
    blocker = fix_blockers.blocker("source_not_final", finality)
    assert blocker["objects"] == [] and "Void" not in blocker["action"]
    assert "the order is not complete in Solidus" in blocker["action"]


def test_a_locked_period_keeps_the_supported_open_period_correction():
    # Review F4: the tool's own guidance proposes the open-period correction; the blocker must not forbid it.
    summary = fix_blockers.for_model(fix_blockers.blocker("period_locked", {}), card_shown=False)
    assert "Do not try another write path" not in summary["say"]
    assert "open-period correction" in summary["what_to_do"]
    for code in fix_blockers.KNOWN - {"period_locked"}:
        assert (
            "Do not try another write path"
            in fix_blockers.for_model(fix_blockers.blocker(code, {}), card_shown=False)["say"]
        )


def test_a_payment_that_differs_from_the_total_is_named():
    # Review R2-F2: the reallocation guard also refuses when Solidus's paid amount differs from its total.
    source = _source(adjustments=[], line_items=[], total="799.00", payment_total="798.00")
    finality = fix_blockers.source_finality(source, require_paid=True)
    assert finality["checks"] == ["payment_differs_from_total"]
    blocker = fix_blockers.blocker("source_not_final", finality)
    assert (
        "Void" not in blocker["action"]
        and "the amount paid in Solidus differs from the order total" in blocker["action"]
    )


def test_unreadable_amounts_are_named():
    finality = fix_blockers.source_finality(_source(adjustments=[], line_items=[], total=None))
    assert finality["checks"] == ["amounts_unreadable"]


def test_a_refusal_with_no_identified_check_never_prescribes_voiding_adjustments():
    blocker = fix_blockers.blocker("source_not_final", {"checks": [], "open_adjustments": []})
    assert "Void" not in blocker["action"] and "not identified" in blocker["action"]
