"""Resolver wiring (M1): the configured credit treatment reaches every order an approved skill covers.

Aiden, 2026-10-06:
- an unpaid invoice gets the credit memo too, applied to the invoice (no invoice edit);
- the seed skill is approved, so an order whose single adjustment carries another label
  (R231821517, "reseller discount") gets the same credit memo the configured label gets.

Accounts, item and book still come from the audited per-connection profile; the skill only
lets the order's own label stand in for the configured one and supplies the memo template.
"""

from copy import deepcopy

import pytest

from app.services.transaction_ops import skills
from app.services.transaction_ops.sales_credit import build_candidate
from tests.test_sales_credit import inputs

SEED = "unbooked-solidus-adjustment"


def _library(*, status="approved", item="50", memo="{order} {adjustment_label}"):
    seed = deepcopy(skills.load_library()[SEED])
    seed.update(status=status)
    seed["change"]["lines"][0]["item"] = item
    seed["change"]["memo"] = memo
    return {SEED: seed}


@pytest.fixture
def library(monkeypatch):
    current = {"value": _library()}
    monkeypatch.setattr(skills, "load_library", lambda directory=None: current["value"])
    return current


def _relabelled(label="reseller discount", **kwargs):
    d = inputs(**kwargs)
    for adjustment in d["source"]["adjustments"]:
        adjustment["label"] = label
    return d


# --- unpaid invoices get the credit memo -----------------------------------------------------


def test_an_unpaid_invoice_gets_the_credit_memo_applied_to_it(library):
    p = build_candidate(**inputs(paid="0"))
    assert p["kind"] == "sales_adjustment_credit"
    assert (p["record_type"], p["mutation_type"]) == ("creditmemo", "create")
    assert p["proposed_fields"]["apply"]["items"] == [{"doc": {"id": "20"}, "apply": True, "amount": 5.0}]
    # The invoice stays open for what Solidus charged; nothing edits the invoice itself.
    assert p["expected_after"]["invoice_remaining"] == "101.00"
    assert p["expected_after"]["net_invoice_total"] == "101"


# --- an approved skill covers another label --------------------------------------------------


def test_an_approved_skill_lets_the_orders_own_label_take_the_configured_treatment(library):
    p = build_candidate(**_relabelled())
    f = p["proposed_fields"]
    assert f["memo"] == "R123456789 reseller discount"
    assert f["item"]["items"] == [{"item": {"id": "50"}, "rate": 5.0, "amount": 5.0, "isTaxable": False}]
    assert f["account"] == {"id": "100"}  # AR from the configured profile, never from the skill
    assert p["skill"] == {"name": SEED, "version": 1}
    assert SEED in p["approval_basis"]


def test_the_configured_label_needs_no_skill(library):
    library["value"] = {}
    p = build_candidate(**inputs())
    assert p["proposed_fields"]["memo"] == "R123456789 Reseller Adjustment 5%"
    assert p.get("skill") is None


def test_an_unpaid_invoice_with_another_label_gets_the_credit_memo(library):
    p = build_candidate(**_relabelled(paid="0"))
    assert p["proposed_fields"]["memo"] == "R123456789 reseller discount"
    assert p["expected_after"]["invoice_remaining"] == "101.00"


@pytest.mark.parametrize(
    "variant",
    [
        "no_skill",
        "proposed",
        "retired",
        "other_item",  # the skill books another item than the configured treatment
        "two_skills",  # never guess between skills
    ],
)
def test_another_label_without_exactly_one_approved_matching_skill_gets_no_candidate(library, variant):
    if variant == "no_skill":
        library["value"] = {}
    elif variant in ("proposed", "retired"):
        library["value"] = _library(status=variant)
    elif variant == "other_item":
        library["value"] = _library(item="1471")
    else:
        twin = deepcopy(_library()[SEED])
        twin["name"] = "twin"
        library["value"] = {**_library(), "twin": twin}
    assert build_candidate(**_relabelled()) is None


def test_two_adjustments_are_never_relabelled_by_a_skill(library):
    d = _relabelled()
    extra = deepcopy(d["source"]["adjustments"][0])
    extra.update(id="99", label="another reason")
    d["source"]["adjustments"].append(extra)
    assert build_candidate(**d) is None


def test_a_skill_without_a_memo_template_uses_order_and_label(library):
    library["value"] = _library(memo=None)
    assert build_candidate(**_relabelled())["proposed_fields"]["memo"] == "R123456789 reseller discount"


def test_the_seed_skill_is_approved_with_its_approver():
    seed = skills.load_library()[SEED]
    assert seed["status"] == "approved"
    assert seed["approved_by"] == "Aiden" and str(seed["approved_at"]) == "2026-10-06"
