"""Resolver skills (spec 2026-10-01 §5.5, block B7): the skill format and finding the skill for a case."""

from __future__ import annotations

import copy
import textwrap

import pytest

from app.services.transaction_ops import skills

# Shapes from case_file.build_case_file (B5) and resolver_reads.chain_read (B6).
CASE_FILE = {
    "case": {"id": "c1", "order": "R100000001", "status": "open"},
    "comparison": {"metrics": {"order_total": {"solidus": "95.18", "netsuite": "100.00", "difference": "-4.82"}}},
    "facts": {"adjustments_equal_to_difference": ["Fix Order Status"]},
}
CHAIN = {
    "top": "10",
    "complete": True,
    "documents": [
        {"id": "10", "type": "sales order", "number": "R100000001", "total": 100.0, "created_from": None, "depth": 0},
        {"id": "11", "type": "customer deposit", "number": "CD1", "total": 100.0, "created_from": "10", "depth": 1},
        {"id": "12", "type": "invoice", "number": "INV1", "total": 100.0, "created_from": "10", "depth": 1},
    ],
}
SEED = "unbooked-solidus-adjustment"


def _approved(library):
    return {name: {**skill, "status": "approved"} for name, skill in library.items()}


def _find(case=CASE_FILE, chain=CHAIN, **kwargs):
    return skills.skill_find(case, chain, library=_approved(skills.load_library()), **kwargs)


def test_the_seed_skill_loads_with_its_evidence_and_awaits_approval():
    seed = skills.load_library()[SEED]
    assert seed["status"] == "proposed" and seed["version"] == 1
    assert seed["diagnosis"] == "needs_credit_memo" and seed["action"] == "create"
    assert any("CM11788" in e for e in seed["evidence"])


def test_an_unbooked_adjustment_gets_the_credit_memo_with_amounts_from_the_case():
    match = _find()["match"]
    assert match["skill"] == SEED
    assert match["change"] == {
        "record_type": "creditMemo",
        "created_from": {"type": "invoice", "id": "12", "number": "INV1"},
        "lines": [{"item": "1471", "amount": "4.82"}],
        "memo": "R100000001 Fix Order Status",
    }
    assert all(check["passed"] for check in match["checks"])


@pytest.mark.parametrize(
    "mutate, failed",
    [
        (lambda c, ch: c["facts"].update(adjustments_equal_to_difference=[]), "adjustment_equals_difference"),
        (lambda c, ch: c["facts"].update(adjustments_equal_to_difference=["A", "B"]), "adjustment_equals_difference"),
        (lambda c, ch: c["comparison"]["metrics"]["order_total"].update(difference="4.82"), "solidus_below_netsuite"),
        (lambda c, ch: ch.update(complete=False), "chain_complete"),
        (lambda c, ch: ch["documents"].pop(), "one_invoice_from_the_order"),
        (
            lambda c, ch: ch["documents"].append(
                {"id": "13", "type": "invoice", "number": "INV2", "total": 1.0, "created_from": "10", "depth": 1}
            ),
            "one_invoice_from_the_order",
        ),
        (
            lambda c, ch: ch["documents"].append(
                {"id": "14", "type": "credit memo", "number": "CM1", "total": -4.82, "created_from": "12", "depth": 2}
            ),
            "no_credit_from_the_invoice",
        ),
    ],
)
def test_a_case_that_fails_a_check_gets_no_skill_and_the_failed_check_is_named(mutate, failed):
    case, chain = copy.deepcopy(CASE_FILE), copy.deepcopy(CHAIN)
    mutate(case, chain)
    result = _find(case, chain)
    assert result["match"] is None
    near = {n["skill"]: n for n in result["near"]}
    assert failed in {f["check"] for f in near[SEED]["failed"]}


def test_a_proposed_skill_is_never_applied_only_reported():
    result = skills.skill_find(CASE_FILE, CHAIN, library=skills.load_library())
    assert result["match"] is None and result["awaiting_approval"] == [SEED]


def test_missing_evidence_fails_the_check_instead_of_raising():
    result = _find({"case": {"order": "R1"}}, {"documents": []})
    assert result["match"] is None


# --- the format: a bad skill file fails loudly at load ------------------------------------


def _write(tmp_path, front):
    (tmp_path / "x.md").write_text("---\n" + textwrap.dedent(front) + "---\nProse.\n")
    return tmp_path


GOOD = """\
name: x
version: 1
status: proposed
cause: c
diagnosis: needs_credit_memo
action: create
checks: [solidus_below_netsuite]
change:
  record_type: creditMemo
  created_from: invoice
  lines: [{item: "1471", amount: difference}]
  memo: "{order} {adjustment_label}"
verify: [v]
evidence: [e]
"""


def test_a_well_formed_skill_file_loads(tmp_path):
    assert skills.load_library(_write(tmp_path, GOOD))["x"]["prose"] == "Prose."


@pytest.mark.parametrize(
    "replace, why",
    [
        (("checks: [solidus_below_netsuite]", "checks: [made_up_check]"), "unknown check"),
        (("status: proposed", "status: live"), "status"),
        (("diagnosis: needs_credit_memo", "diagnosis: vibes"), "diagnosis"),
        (("amount: difference", "amount: 12.50"), "amount"),
        (("version: 1", "version: one"), "version"),
        (('memo: "{order} {adjustment_label}"', 'memo: "{order} {secret}"'), "memo"),
    ],
)
def test_a_malformed_skill_file_fails_loudly(tmp_path, replace, why):
    with pytest.raises(ValueError, match=why):
        skills.load_library(_write(tmp_path, GOOD.replace(*replace)))


def test_an_approved_skill_names_who_approved_it(tmp_path):
    with pytest.raises(ValueError, match="approved_by"):
        skills.load_library(_write(tmp_path, GOOD.replace("status: proposed", "status: approved")))
