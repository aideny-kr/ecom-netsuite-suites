from copy import deepcopy

import pytest

from app.services.transaction_ops.sales_credit import build_candidate
from tests.test_sales_credit import inputs


@pytest.mark.parametrize(
    "variant",
    [
        "missing_location",
        "invalid_location",
        "inactive",
        "wrong_subsidiary",
        "truncated_subsidiaries",
        "missing_subsidiaries",
        "incomplete_lines",
        "no_lines",
        "line_conflict",
        "line_only_department",
        "wrong_record_identity",
        "missing_active_flag",
    ],
)
def test_credit_refuses_unknown_or_ambiguous_classification(variant):
    data = inputs()
    s = data["support"]
    inv = s["invoice"]
    loc = s["classification_records"]["location"]
    if variant == "missing_location":
        inv.pop("location")
    if variant == "invalid_location":
        inv["location"] = {"id": "not-an-id"}
    if variant == "inactive":
        loc["isInactive"] = True
    if variant == "wrong_subsidiary":
        loc["subsidiary"]["items"] = [{"id": "2"}]
    if variant == "truncated_subsidiaries":
        loc["subsidiary"]["hasMore"] = True
    if variant == "missing_subsidiaries":
        loc.pop("subsidiary")
    if variant == "incomplete_lines":
        s["classification_lines_complete"] = False
    if variant == "no_lines":
        s["classification_lines"] = []
    if variant == "line_conflict":
        s["classification_lines"] = [{"location": {"id": "99"}}]
    if variant == "line_only_department":
        inv.pop("department")
        s["classification_lines"] = [{"department": {"id": "18"}}]
    if variant == "wrong_record_identity":
        loc["id"] = "99"
    if variant == "missing_active_flag":
        loc.pop("isInactive")
    assert build_candidate(**data) is None


def test_credit_uses_target_invoice_not_reference_credit_classifications():
    data = inputs()
    data["support"]["reference_credit"]["location"] = {"id": "999"}
    data["support"]["classification_lines"] = [{"location": {"id": "30"}, "department": {"id": "18"}}]
    p = build_candidate(**data)
    assert p["proposed_fields"]["location"] == {"id": "30"}
    assert p["proposed_fields"]["department"] == {"id": "18"}


def test_class_is_preserved_when_verified_and_optional_department_may_be_absent():
    data = inputs()
    s = data["support"]
    s["invoice"].pop("department")
    s["invoice"]["class"] = {"id": "7", "refName": "Channel"}
    s["classification_records"]["class"] = {**deepcopy(s["classification_records"]["location"]), "id": "7"}
    p = build_candidate(**data)
    assert p["proposed_fields"]["class"] == {"id": "7"}
    assert "department" not in p["proposed_fields"]
