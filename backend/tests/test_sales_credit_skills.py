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
from app.services.transaction_ops.sales_credit import build_candidate, external_id
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
    """Review round 1 (F3): the split keeps the header and posting key consistent, so only the
    single-adjustment rule can refuse it."""
    d = _relabelled()
    first = d["source"]["adjustments"][0]
    second = deepcopy(first)
    first["amount"], second["amount"] = "-3", "-2"
    second.update(id="99", label="another reason")
    d["source"]["adjustments"].append(second)
    d["support"]["duplicates"]["posting_key"] = external_id(
        "tenant", d["review"]["scope"], d["source"], d["support"]["invoice"]["id"]
    )
    assert build_candidate(**d) is None
    d["review"]["sales_credit_profile"]["source_adjustment_label"] = "another reason"
    for adjustment in d["source"]["adjustments"]:
        adjustment["label"] = "another reason"
    assert build_candidate(**d) is not None, "the same split under the configured label must build"


def test_a_skill_without_a_memo_template_uses_order_and_label(library):
    library["value"] = _library(memo=None)
    assert build_candidate(**_relabelled())["proposed_fields"]["memo"] == "R123456789 reseller discount"


def test_the_seed_skill_is_approved_with_its_approver():
    seed = skills.load_library()[SEED]
    assert seed["status"] == "approved"
    assert seed["approved_by"] == "Aiden" and str(seed["approved_at"]) == "2026-10-06"


# --- review round 1 ---------------------------------------------------------------------------


def test_r1_a_skill_memo_without_the_order_number_never_builds(library):
    """F2: verification finds the posted credit by the order number in its memo."""
    library["value"] = _library(memo="{adjustment_label}")
    assert build_candidate(**_relabelled()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["unchanged", "retired", "replaced"])
async def test_r1_approval_refuses_a_card_whose_skill_changed(library, variant):
    """F1: approval rebuilds the card; a retired or replaced skill must not pass as unchanged."""
    import json
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from app.services.transaction_ops.sales_credit import validate_approved
    from tests.test_accounting_approval_flow import inputs as tool_inputs

    data = _relabelled()
    now = datetime.now(timezone.utc)
    data["now"] = now
    data["support"]["observed_at"] = now.isoformat()
    for refund in data["support"]["refunds"].values():
        refund["observed_at"] = now.isoformat()
    data["review"]["native_mcp_connector_id"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    p = build_candidate(**data)
    assert p["skill"] == {"name": SEED, "version": 1}
    if variant == "retired":
        library["value"] = _library(status="retired")
    elif variant == "replaced":
        replacement = deepcopy(_library()[SEED])
        replacement.update(name="replacement", version=2)
        library["value"] = {SEED: _library(status="retired")[SEED], "replacement": replacement}
    name, body = tool_inputs(p)
    case = SimpleNamespace(
        status="open",
        order_reference=p["order_reference"],
        id="case",
        scope_json=p["scope"],
        latest_report_json=data["report"],
    )
    connector = SimpleNamespace(server_url="https://123.suitetalk.api.netsuite.com/services/mcp")
    db = AsyncMock()
    db.scalar.return_value = case
    review, source, support = (deepcopy(data[k]) for k in ("review", "source", "support"))
    with (
        patch("app.services.mcp_connector_service.get_mcp_connector", AsyncMock(return_value=connector)),
        patch("app.services.transaction_ops.accounting_review.accounting_context", AsyncMock(return_value=review)),
        patch("app.services.transaction_ops.tax_correction.refresh_source", AsyncMock(return_value=source)),
        patch(
            "app.services.transaction_ops.accounting_evidence.collect_accounting_evidence", AsyncMock(return_value={})
        ),
        patch("app.services.transaction_ops.commercial_credits.collect_commercial_credits", AsyncMock()),
        patch("app.services.transaction_ops.sales_credit.collect_support", AsyncMock(return_value=support)),
    ):
        if variant == "unchanged":
            await validate_approved(db, "tenant", name, body, p)
        else:
            with pytest.raises(ValueError):
                await validate_approved(db, "tenant", name, body, p)
    assert json.loads(body["data"])["memo"] == "R123456789 reseller discount"


def test_r1_a_group_never_mixes_skill_covered_and_configured_credits(library):
    """F1: group approval binds one treatment; the skill is part of it. Cards without a skill keep
    their treatment id unchanged."""
    from app.services.transaction_ops.accounting_treatments import treatment_batches

    plain, covered = build_candidate(**inputs(paid="25")), build_candidate(**_relabelled(paid="25"))
    members = [
        {"case_id": kind, "confirmation_id": kind, "card": {"accounting_review": review}}
        for kind, review in (("plain", plain), ("covered", covered))
    ]
    batches = treatment_batches(members)
    assert len(batches) == 2
    by_case = {b["case_ids"][0]: b["treatment"] for b in batches}
    assert "skill" not in by_case["plain"]
    assert by_case["covered"]["skill"] == {"name": SEED, "version": 1}
