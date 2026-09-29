from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.services.transaction_ops.netsuite_refunds import collect_refunds
from app.services.transaction_ops.order_reconciliation import reconcile_order
from app.services.transaction_ops.policy_equivalence import changed_reasons, evaluate
from tests.test_refund_adjustment_matching import REFERENCE, CreditReader, balance_case, reader_profile

NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)
STAMP = "2026-09-22T12:00:00+00:00"


def snapshots():
    before = {
        "source_connection_id": "solidus",
        "source_step_id": None,
        "netsuite_connection_id": "native",
        "netsuite_account_id": "123-sb1",
        "subsidiary_id": "5",
        "record_type": "salesOrder",
        "target_step_id": None,
        "evidence_contract_version": 1,
        "destination_discovery_version": 2,
        "mapping_json": {
            "refund_adjustments": {
                "schema_version": 1,
                "account_id": "123-sb1",
                "subsidiary_id": "5",
                "tax_reversal_reason_ids": ["102"],
                "tax_item_accounts": {"80": "90"},
                "tax_accounts": ["90"],
            }
        },
    }
    after = deepcopy(before)
    after["mapping_json"]["refund_adjustments"]["tax_reversal_reason_ids"].append("4")
    return before, after


def saved_report():
    before, _ = snapshots()
    return {
        "order_reference": REFERENCE,
        "_observation": {"final": True, "observed_at": STAMP},
        "source": {"currency": "USD", "observed_at": STAMP},
        "targets": [{"observed_at": STAMP}],
        "refund_evidence": {
            "source": {
                "complete": True,
                "events_complete": True,
                "events": [],
                "observed_at": STAMP,
                "order_reference": REFERENCE,
                "currency": "USD",
            },
            "target": {
                "complete": True,
                "provider": "netsuite",
                "connection_id": "native",
                "account_id": "123-sb1",
                "subsidiary_id": "5",
                "observed_at": STAMP,
                "order_reference": REFERENCE,
                "currency": "USD",
                "request_links": [],
                "tax_adjustments": [],
            },
        },
        "balance": {
            "status": "matched",
            "missing_metrics": [],
            "amounts": {
                key: {"source": "1.00", "target": "1.00", "delta": "0.00"} for key in ("order_total", "tax", "refunds")
            },
        },
    }


def link(reason="102"):
    return {
        "reason_id": reason,
        "request_id": "20",
        "source_refund_id": "30",
        "amount": "1.00",
        "stage": "refund_verified",
        "credit_memo_id": "40",
        "refund_id": "50",
    }


def test_equivalence_preserves_original_amounts_timestamps_and_does_not_mutate_inputs():
    before, after = snapshots()
    report = saved_report()
    copy = deepcopy((before, after, report))
    result = evaluate(report, before, changed_reasons(before, after), evaluated_at=NOW)
    assert result["status"] == "equivalent"
    assert result["balance"] == report["balance"]
    assert result["original_observed_at"] == STAMP
    assert (before, after, report) == copy
    assert "hybrid_classification" not in result


@pytest.mark.parametrize("status", ["matched", "difference"])
def test_previously_matched_orders_with_changed_reason_need_refresh(status):
    before, after = snapshots()
    report = saved_report()
    report["balance"]["status"] = status
    report["refund_evidence"]["target"]["request_links"] = [link("4")]
    assert evaluate(report, before, changed_reasons(before, after), evaluated_at=NOW)["status"] == "affected"


def test_reason_removals_are_also_affected():
    before, after = snapshots()
    report = saved_report()
    report["refund_evidence"]["target"]["request_links"] = [link("4")]
    assert changed_reasons(after, before) == {"4"}
    assert evaluate(report, after, changed_reasons(after, before), evaluated_at=NOW)["status"] == "affected"


@pytest.mark.parametrize(
    "key",
    [
        "source_connection_id",
        "source_step_id",
        "netsuite_connection_id",
        "netsuite_account_id",
        "subsidiary_id",
        "record_type",
        "target_step_id",
        "evidence_contract_version",
        "destination_discovery_version",
    ],
)
def test_every_scope_and_contract_change_refuses_reuse(key):
    before, after = snapshots()
    after[key] = "other"
    with pytest.raises(ValueError):
        changed_reasons(before, after)


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s["mapping_json"].update(currency_minor_units={"USD": 3}),
        lambda s: s["mapping_json"]["refund_adjustments"].update(tax_accounts=["91"]),
        lambda s: s["mapping_json"].pop("refund_adjustments"),
        lambda s: s["mapping_json"]["refund_adjustments"].update(tax_reversal_reason_ids=[]),
    ],
)
def test_unrelated_policy_and_invalid_profiles_refuse(change):
    before, after = snapshots()
    change(after)
    with pytest.raises(ValueError):
        changed_reasons(before, after)


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(evidence_limits={"code": "evidence_size_limit"}),
        lambda r: r["_observation"].update(final=False),
        lambda r: r["refund_evidence"]["source"].update(events_complete=False),
        lambda r: r["refund_evidence"]["source"].pop("events"),
        lambda r: r["refund_evidence"]["target"].pop("request_links"),
        lambda r: r["refund_evidence"]["target"].pop("tax_adjustments"),
        lambda r: r["refund_evidence"]["target"].update(complete=False),
        lambda r: r["refund_evidence"]["target"].update(account_id="another"),
        lambda r: r["refund_evidence"]["target"].update(currency="GBP"),
        lambda r: r["refund_evidence"]["target"].update(observed_at="2030-01-01T00:00:00Z"),
        lambda r: r["refund_evidence"]["target"].update(observed_at="2026-09-01"),
        lambda r: r["refund_evidence"]["target"].update(request_links=[{}]),
        lambda r: r["refund_evidence"]["target"].update(request_links=[link(None)]),
        lambda r: r["refund_evidence"]["target"].update(request_links=[link("102"), link("102")]),
        lambda r: r["balance"].update(status="incomplete"),
        lambda r: r["balance"]["amounts"]["tax"].update(delta="NaN"),
    ],
)
def test_unknown_is_never_an_unaffected_zero(change):
    before, after = snapshots()
    report = saved_report()
    change(report)
    assert evaluate(report, before, changed_reasons(before, after), evaluated_at=NOW)["status"] == "unknown"


@pytest.mark.parametrize("reason", ["3", "102"])
async def test_frozen_complete_provider_fixtures_take_identical_branches_outside_changed_reason(reason):
    results = []
    for membership in (["102"], ["102", "4"]):
        reader = CreditReader()
        reader.requests[0]["reason_id"] = reason
        profile = reader_profile()
        profile["tax_reversal_reason_ids"] = membership
        results.append(
            await collect_refunds(reader, "1", "1", "1", order_reference=REFERENCE, adjustment_profile=profile)
        )
    assert results[0] == results[1]


def test_deterministic_comparison_has_same_money_and_status_for_unchanged_membership():
    source, target, config, refunds = balance_case()
    old = reconcile_order(source, target, config, refunds=refunds)
    config["mapping_json"]["refund_adjustments"]["tax_reversal_reason_ids"].append("4")
    assert reconcile_order(source, target, config, refunds=refunds) == old
