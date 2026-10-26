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
    assert result["status"] == "equivalent", result
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


def test_runner_built_report_retains_the_fields_needed_for_equivalence():
    from app.services.transaction_ops.normalization import TransactionMapping
    from app.services.transaction_ops.runner import build_report
    from tests.test_transaction_ops_planner import planning_case

    case = planning_case()
    case.targets["lookup"]["count"] = len(case.targets["orders"])
    before, after = snapshots()
    for snapshot in (before, after):
        snapshot.update(
            netsuite_account_id="6738075-sb1",
            subsidiary_id="3",
            netsuite_connection_id=str(case.config.netsuite_connection_id),
        )
        snapshot["mapping_json"].update(case.config.mapping_json)
        snapshot["mapping_json"]["refund_adjustments"].update(account_id="6738075-sb1", subsidiary_id="3")
    refunds = saved_report()["refund_evidence"]
    for side in refunds.values():
        side.update(
            order_reference="R123456789", currency="EUR", amount="0", refund_count=0, observed_at=case.now.isoformat()
        )
    refunds["target"].update(
        connection_id=str(case.config.netsuite_connection_id), account_id="6738075-sb1", subsidiary_id="3"
    )
    # The NetSuite refund read reports invoice credits since 2026-10-01, and apart from them the
    # order's own return credits since C15 (none here).
    empty = {"complete": True, "credits": [], "total": "0", "tax": "0"}
    refunds["target"]["invoice_credits"] = {**empty, "return_credits": dict(empty)}
    report = build_report(
        case.source,
        case.targets,
        before,
        TransactionMapping.model_validate(before["mapping_json"]),
        now=case.now,
        refunds=refunds,
    )
    # These are the two fields attached by the runner/state persistence path.
    report.update(refund_evidence=refunds, _observation={"final": True, "observed_at": case.now.isoformat()})
    result = evaluate(report, before, changed_reasons(before, after), evaluated_at=case.now)
    assert result["status"] == "equivalent", result
    assert result["balance"] == report["balance"]


# 2026-10-01: a credit memo created from the order's invoice can now explain an
# order-total difference. A saved difference read before credits were read cannot be
# reused as-is; only those orders are read again.


def saved_difference(order_total_delta="-4.82", tax_delta="0.00"):
    report = saved_report()
    report["balance"]["status"] = "difference"
    report["balance"]["amounts"]["order_total"] = {"source": "1.00", "target": "5.82", "delta": order_total_delta}
    report["balance"]["amounts"]["tax"]["delta"] = tax_delta
    return report


def test_a_saved_order_total_difference_without_a_credit_read_is_read_again():
    before, _ = snapshots()
    result = evaluate(saved_difference(), before, frozenset(), evaluated_at=NOW)
    assert result == {"status": "affected", "reason": "invoice_credits_not_read"}


def test_a_saved_difference_already_read_with_credits_is_reused():
    before, _ = snapshots()
    report = credits_read(saved_difference())
    assert evaluate(report, before, frozenset(), evaluated_at=NOW)["status"] == "equivalent"


def test_a_saved_difference_with_an_equal_order_total_is_still_reused():
    before, _ = snapshots()
    report = saved_difference(order_total_delta="0.00", tax_delta="0.10")
    report["balance"]["amounts"]["order_total"] = {"source": "1.00", "target": "1.00", "delta": "0.00"}
    assert evaluate(report, before, frozenset(), evaluated_at=NOW)["status"] == "equivalent"


# 2026-10-22 (decided by Aiden; PR #422 review round 2, F4): a settled canceled order and the leftover
# tax on a zero order are matched. A saved difference those rules could change is not reused: a
# canceled order read before its billing and deposits were reported is read again, and so is a zero
# order with a tax difference. Every other saved result keeps reuse.


def credits_read(report):
    # A read since C15: the invoice credits and, apart from them, the order's own return credits.
    empty = {"complete": True, "credits": [], "total": "0", "tax": "0"}
    report["refund_evidence"]["target"]["invoice_credits"] = {**empty, "return_credits": dict(empty)}
    return report


@pytest.mark.parametrize("status", ["cancelled", "unknown"])
def test_a_saved_canceled_difference_without_deposits_is_read_again(status):
    before, _ = snapshots()
    report = credits_read(saved_difference())
    report["source"]["status"] = status
    assert evaluate(report, before, frozenset(), evaluated_at=NOW) == {
        "status": "affected",
        "reason": "canceled_order_evidence_not_read",
    }


def test_a_saved_canceled_difference_read_with_deposits_and_billing_is_reused():
    before, _ = snapshots()
    report = credits_read(saved_difference())
    report["source"]["status"] = "cancelled"
    report["refund_evidence"]["target"].update(billing_ids=[], deposits=[])
    assert evaluate(report, before, frozenset(), evaluated_at=NOW)["status"] == "equivalent"


def test_a_saved_difference_on_an_active_order_keeps_reuse():
    before, _ = snapshots()
    report = credits_read(saved_difference())
    report["source"]["status"] = "fulfilled"
    assert evaluate(report, before, frozenset(), evaluated_at=NOW)["status"] == "equivalent"


def test_a_saved_zero_order_tax_difference_is_read_again():
    before, _ = snapshots()
    report = saved_report()
    report["balance"]["status"] = "difference"
    report["balance"]["amounts"]["order_total"] = {"source": "0.00", "target": "0.00", "delta": "0.00"}
    report["balance"]["amounts"]["tax"] = {"source": "0.88", "target": "0.00", "delta": "0.88"}
    assert evaluate(report, before, frozenset(), evaluated_at=NOW) == {
        "status": "affected",
        "reason": "zero_total_order_tax",
    }


def test_a_saved_match_is_never_affected_by_the_new_rules():
    before, _ = snapshots()
    report = saved_report()
    report["source"]["status"] = "cancelled"
    assert evaluate(report, before, frozenset(), evaluated_at=NOW)["status"] == "equivalent"


# C15: a credit memo created from the order's own return authorization can now explain an
# order-total difference the invoice credits do not. A saved order-total difference read before
# return credits were reported is read again. A saved match keeps reuse: the invoice credits
# are still tried first and alone, so no match they made can change (except on a zero order, C15-R1 below).


def test_the_rule_version_names_the_return_credit_rule():
    from app.services.transaction_ops.policy_equivalence import RULE_VERSION

    assert RULE_VERSION == 4


def test_a_saved_order_total_difference_read_before_return_credits_is_read_again():
    before, _ = snapshots()
    report = saved_difference()
    report["refund_evidence"]["target"]["invoice_credits"] = {"complete": True, "credits": [], "total": "0", "tax": "0"}
    assert evaluate(report, before, frozenset(), evaluated_at=NOW) == {
        "status": "affected",
        "reason": "return_credits_not_read",
    }


@pytest.mark.parametrize(
    "returns",
    [
        {"complete": True, "credits": [], "total": "0", "tax": "0"},
        {"complete": False, "reason": "return_credit_ownership_ambiguous"},
    ],
)
def test_a_saved_difference_read_with_return_credits_is_reused(returns):
    before, _ = snapshots()
    report = credits_read(saved_difference())
    report["refund_evidence"]["target"]["invoice_credits"]["return_credits"] = returns
    assert evaluate(report, before, frozenset(), evaluated_at=NOW)["status"] == "equivalent"


def test_a_saved_difference_whose_invoice_credits_were_unknown_is_reused():
    # Return credits count only with the invoice credits, so an unknown invoice-credit read
    # cannot match under the new rule either.
    before, _ = snapshots()
    report = saved_difference()
    report["refund_evidence"]["target"]["invoice_credits"] = {"complete": False, "reason": "invoice_credit_budget"}
    assert evaluate(report, before, frozenset(), evaluated_at=NOW)["status"] == "equivalent"


def test_a_saved_match_on_invoice_credits_read_before_return_credits_is_reused():
    before, _ = snapshots()
    report = saved_report()
    report["refund_evidence"]["target"]["invoice_credits"] = {
        "complete": True,
        "credits": [
            {"id": "30", "number": "CM30", "invoice_id": "9", "total": "4.82", "tax": "0", "posting": "income"}
        ],
        "total": "4.82",
        "tax": "0",
        "invoices": {"ids": ["9"], "total": "5.82", "tax": "0"},
    }
    report["balance"]["adjustments"] = [
        {"kind": "invoice_credit_memos", "status": "verified", "total": "4.82", "tax": "0.00", "credits": []}
    ]
    result = evaluate(report, before, frozenset(), evaluated_at=NOW)
    assert result["status"] == "equivalent" and result["balance"] == report["balance"]


# C15-R1: on a zero order whose source still reports tax, a credit tier counts only under the zero-order
# rule on the credited values, never by equalling Solidus's stale tax. A saved credit match that left
# NetSuite tax on a zero order (an invoice-credit match before this rule) is read again; one that cleared
# the tax, and every credit match on an order with a source total, keeps reuse.


def saved_zero_order_credit_match(tax, adjustments):
    report = saved_report()
    report["refund_evidence"]["target"]["invoice_credits"] = {"complete": True, "credits": [], "total": "0", "tax": "0"}
    report["balance"]["amounts"]["order_total"] = {"source": "0.00", "target": "0.00", "delta": "0.00"}
    report["balance"]["amounts"]["tax"] = {"source": tax, "target": tax, "delta": "0.00"}
    report["balance"]["adjustments"] = [{"kind": kind, "status": "verified"} for kind in adjustments]
    return report


def test_a_saved_credit_match_that_left_tax_on_a_zero_order_is_read_again():
    before, _ = snapshots()
    report = saved_zero_order_credit_match("20.00", ["invoice_credit_memos"])
    assert evaluate(report, before, frozenset(), evaluated_at=NOW) == {
        "status": "affected",
        "reason": "credited_zero_order_tax",
    }


@pytest.mark.parametrize(
    "tax,adjustments",
    [
        ("0.00", ["invoice_credit_memos", "zero_total_order_tax"]),  # the credit cleared NetSuite's tax
        ("0.00", ["invoice_credit_memos"]),  # a zero order with no tax at all
        ("20.00", []),  # a plain comparison: no credit counted
    ],
)
def test_a_saved_zero_order_match_the_credit_rule_cannot_change_is_reused(tax, adjustments):
    before, _ = snapshots()
    report = saved_zero_order_credit_match(tax, adjustments)
    assert evaluate(report, before, frozenset(), evaluated_at=NOW)["status"] == "equivalent"
