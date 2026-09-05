from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.transaction_ops import planner
from app.services.transaction_ops.netsuite_actions import prepare_correction
from app.services.transaction_ops.normalization import TransactionMapping
from app.services.transaction_ops.runner import build_report
from tests.test_transaction_ops_netsuite_actions import target as target_example
from tests.test_transaction_ops_runner import source_order


def planning_case():
    now = datetime.now(timezone.utc)
    config = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        source_step_id=uuid4(),
        netsuite_connection_id=uuid4(),
        target_step_id=uuid4(),
        netsuite_account_id="6738075_SB1",
        subsidiary_id="3",
        record_type="salesorder",
        enabled=True,
        mapping_json={
            "action_mode": "propose_actions",
            "reference_field": "tranid",
            "currency_minor_units": {"EUR": 2},
            "business_entity_subsidiaries": {"legacy": "3"},
        },
    )
    source = source_order()
    source.update(read_at=now.isoformat(), celigo_step_id=str(config.source_step_id), connection_id="source-connection")
    source["orders"][0].update(number="R123456789", currency="EUR", updated_at=(now - timedelta(hours=1)).isoformat())
    target = target_example()
    target["header"].update(
        total="90", subtotal="90", taxTotal="0", shippingCost="0", custbody_fw_solidus_order_total="90"
    )
    target["lines"][0].update(quantity="1", rate="90", amount="90", custcol_fw_vat_amount="0", taxRate1="0")
    targets = {
        "provider": "netsuite",
        "orders": [target],
        "lookup": {"complete": True},
        "observed_at": now.isoformat(),
        "scope": {"account_id": "6738075-sb1", "subsidiary_id": "3"},
    }
    snapshot = {key: getattr(config, key) for key in ("netsuite_account_id", "subsidiary_id", "record_type")}
    report = build_report(source, targets, snapshot, TransactionMapping.model_validate(config.mapping_json), now=now)
    plan = prepare_correction(target, report["source"], now=now)
    guard = {"snapshot": plan.before_json, "actions_enabled": True, "observed_at": now.isoformat()}
    return SimpleNamespace(now=now, config=config, source=source, targets=targets, report=report, guard=guard)


def proposal(case, **kwargs):
    return planner.plan_proposal(case.report, case.targets, case.config, guard=case.guard, now=case.now, **kwargs)


def test_finding_produces_exact_guarded_proposal_with_original_currency():
    case = planning_case()
    request = proposal(case)
    assert request.action == "correct_amounts" and request.currency == "EUR"
    assert request.before_json == case.guard["snapshot"]
    assert request.after_json["expected_totals"]["total"] == "100"
    assert request.evidence_json["report"] == case.report
    assert request.evidence_json["source_version"] == case.report["source"]["updated_at"]
    assert len(request.evidence_fingerprint) == 64


def test_observation_time_does_not_reissue_identical_economic_work():
    case = planning_case()
    first = proposal(case)
    case.report = deepcopy(case.report)
    for snapshot in [case.report["source"], *case.report["targets"], case.report["lookup"]]:
        snapshot["observed_at"] = (case.now + timedelta(seconds=5)).isoformat()
    case.guard["observed_at"] = (case.now + timedelta(seconds=5)).isoformat()
    case.now += timedelta(seconds=5)
    assert proposal(case).evidence_fingerprint == first.evidence_fingerprint


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.config.mapping_json.update(action_mode="detect_only"),
        lambda c: setattr(c.config, "enabled", False),
        lambda c: c.guard.update(actions_enabled=False),
        lambda c: c.guard["snapshot"].update(currency="1"),
        lambda c: c.guard["snapshot"].update(version="changed"),
        lambda c: c.targets["orders"][0]["periods"]["items"][0].update(closed="T"),
        lambda c: c.report["comparison"].update(recommended_action="gather_evidence"),
    ],
)
def test_no_write_proposal_for_disabled_incomplete_changed_or_closed_evidence(mutation):
    case = planning_case()
    mutation(case)
    with pytest.raises(planner.PlanningError):
        proposal(case)


def test_source_version_is_bound_even_when_money_unchanged():
    case = planning_case()
    first = proposal(case)
    case.report["source"]["updated_at"] = (case.now - timedelta(minutes=30)).isoformat()
    # Recompute rather than trust an injected recommendation/fingerprint.
    second = proposal(case)
    assert second.evidence_fingerprint != first.evidence_fingerprint


def matched_case():
    case = planning_case()
    target = case.targets["orders"][0]
    target["header"].update(total="100", subtotal="100", custbody_fw_solidus_order_total="100")
    target["lines"][0].update(rate="100", amount="100")
    case.report = build_report(
        case.source,
        case.targets,
        {"netsuite_account_id": case.config.netsuite_account_id, "subsidiary_id": "3", "record_type": "salesorder"},
        TransactionMapping.model_validate(case.config.mapping_json),
        now=case.now,
    )
    case.celigo = {
        "provider": "celigo",
        "complete": True,
        "order_reference": "R123456789",
        "fingerprint": "c" * 64,
        "configuration_fingerprint": "f" * 64,
        "observed_at": case.now.isoformat(),
        "scope": {
            "connection_id": "celigo-connection",
            "flow_id": "flow",
            "import_id": "import",
            "account_id": "6738075-sb1",
            "subsidiary_id": "3",
            "record_type": "salesorder",
            "operation": "add",
        },
        "error": {"error_id": "error-1", "retry_data_key": "retry-1", "kind": "duplicate_transaction"},
    }
    return case


def test_matching_transaction_and_exact_duplicate_create_error_produce_resolution():
    case = matched_case()
    request = proposal(case, celigo=case.celigo)
    assert request.action == "resolve_celigo_error"
    assert request.before_json == {"celigo_error_id": "error-1", "celigo_error_state": "open"}
    assert request.after_json == {"celigo_error_id": "error-1", "celigo_error_state": "resolved"}
    assert request.evidence_json["celigo"] == case.celigo


@pytest.mark.parametrize(
    "field,value",
    [("account_id", "other"), ("subsidiary_id", "4"), ("operation", "update"), ("record_type", "invoice")],
)
def test_false_alarm_requires_exact_destination_scope(field, value):
    case = matched_case()
    case.celigo["scope"][field] = value
    with pytest.raises(planner.PlanningError):
        proposal(case, celigo=case.celigo)


def test_absent_open_error_is_not_a_resolution_proposal():
    case = matched_case()
    case.celigo.update(complete=False)
    with pytest.raises(planner.PlanningError):
        proposal(case, celigo=case.celigo)


def test_source_fingerprint_ignores_decimal_scale_and_equivalent_timezones():
    case = planning_case()
    original = case.report["source"]
    changed = deepcopy(original)
    changed["total"] = "100.000"
    changed["lines"][0]["net"] = "100.00"
    changed["updated_at"] = (
        datetime.fromisoformat(original["updated_at"]).astimezone(timezone(timedelta(hours=-7))).isoformat()
    )
    assert planner.source_fingerprint(changed) == planner.source_fingerprint(original)
