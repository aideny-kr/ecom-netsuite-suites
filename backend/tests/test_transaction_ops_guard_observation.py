"""An omitted REST amount requires a complete, matching native observation."""

from copy import deepcopy

import pytest

from app.services.transaction_ops import netsuite_actions as mod
from app.services.transaction_ops import planner
from tests.test_transaction_ops_netsuite_actions import source, target
from tests.test_transaction_ops_planner import planning_case, proposal


@pytest.mark.parametrize("absent", [True, False])
def test_matching_guard_supplies_omitted_handling_without_mutating_rest(absent):
    evidence, desired = target(), source()
    original = mod.prepare_correction(evidence, desired)
    if absent:
        del evidence["header"]["handlingCost"]
    else:
        evidence["header"]["handlingCost"] = None
    untouched = deepcopy(evidence)
    prepared = mod.prepare_correction(evidence, desired, native_snapshot=original.before_json)
    assert prepared.before_json == original.before_json
    assert prepared.after_json == original.after_json
    assert evidence == untouched


@pytest.mark.parametrize(
    "field,value",
    [
        ("record_id", "64"),
        ("version", "2026-09-04T11:00:00.000Z"),
        ("entity", "41"),
        ("subsidiary", "4"),
        ("currency", "1"),
        ("exchangerate", "1.2"),
        ("period_id", "100"),
        ("total", "111"),
        ("order_reference", "R987654321"),
        ("reference_field", "externalid"),
    ],
)
def test_observation_from_changed_record_cannot_fill_missing_amount(field, value):
    evidence, desired = target(), source()
    native = mod.prepare_correction(evidence, desired).before_json
    del evidence["header"]["handlingCost"]
    native[field] = value
    with pytest.raises(mod.NetSuiteActionError, match="guard_evidence_changed"):
        mod.prepare_correction(evidence, desired, native_snapshot=native)


def test_observation_with_changed_line_cannot_fill_missing_amount():
    evidence, desired = target(), source()
    native = mod.prepare_correction(evidence, desired).before_json
    del evidence["header"]["handlingCost"]
    native["lines"][0]["item"] = "601"
    with pytest.raises(mod.NetSuiteActionError, match="guard_evidence_changed"):
        mod.prepare_correction(evidence, desired, native_snapshot=native)


@pytest.mark.parametrize("snapshot", [None, {}, {"handlingcost": None}, {"handlingcost": True}])
def test_missing_handling_stays_unknown_without_native_amount(snapshot):
    evidence = target()
    del evidence["header"]["handlingCost"]
    with pytest.raises(mod.NetSuiteActionError, match="unknown_amount"):
        mod.prepare_correction(evidence, source(), native_snapshot=snapshot)


def test_explicit_rest_handling_is_not_overridden_by_native_zero():
    evidence, desired = target(), source()
    native = mod.prepare_correction(evidence, desired).before_json
    evidence["header"]["handlingCost"] = "3"
    with pytest.raises(mod.NetSuiteActionError, match="unsupported_handling_or_discount"):
        mod.prepare_correction(evidence, desired, native_snapshot=native)


def test_native_nonzero_handling_remains_unsupported():
    evidence, desired = target(), source()
    native = mod.prepare_correction(evidence, desired).before_json
    del evidence["header"]["handlingCost"]
    native["handlingcost"] = "3"
    with pytest.raises(mod.NetSuiteActionError, match="unsupported_handling_or_discount"):
        mod.prepare_correction(evidence, desired, native_snapshot=native)


def test_planner_supplies_fresh_complete_guard_observation():
    case = planning_case(inventory=True, assessment=True)
    del case.targets["orders"][0]["header"]["handlingCost"]
    request = proposal(case)
    assert request.before_json == case.guard["snapshot"]
    assert request.before_json["handlingcost"] == "0"


def test_planner_rejects_stale_observation_before_using_missing_amount():
    case = planning_case()
    del case.targets["orders"][0]["header"]["handlingCost"]
    case.guard["observed_at"] = "2026-01-01T00:00:00Z"
    with pytest.raises(planner.PlanningError, match="guard_stale"):
        proposal(case)
