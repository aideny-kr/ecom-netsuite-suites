"""The resolver's first look at a case: one compact file from saved evidence only (spec B5).

Shaped on R000227174 (2026-09-30): Solidus has a "Fix Order Status" adjustment of -4.82, the
sales order is 4.82 higher, and NetSuite already holds CM11788 for 4.82 created from INV363632.
"""

import json
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.services.transaction_ops import case_file

CASE = {
    "id": "aa4a5c59-95b0-4038-98e6-58bd08642e33",
    "order_reference": "R000227174",
    "status": "open",
    "scope": {"subsidiary_id": "1", "netsuite_account_id": "6738075", "record_type": "salesorder"},
    "last_observed_at": "2026-09-29T17:20:49+00:00",
}

REPORT = {
    "balance": {
        "status": "difference",
        "reason": "amounts_differ",
        "currency": "USD",
        "amounts": {
            "order_total": {"source": "2769.84", "target": "2774.66", "delta": "-4.82"},
            "tax": {"source": "195.66", "target": "195.66", "delta": "0.00"},
            "refunds": {"source": "0.00", "target": "0.00", "delta": "0.00"},
        },
    },
    "targets": [
        {
            "record_id": "14388301",
            "total": "2774.66",
            "tax": "195.66",
            "subtotal": "2579.0",
            "discount": "0.0",
            "shipping": "0.0",
            "status": "fulfilled",
            "lines": [
                {"key": "line:60702230", "quantity": "1.0", "net": "1181.0", "tax": "89.59"},
                {"key": "line:60702231", "quantity": "2.0", "net": "810.0", "tax": "61.45"},
            ],
        }
    ],
    "refund_evidence": {
        "target": {
            "amount": "0.00",
            "refund_count": 0,
            "complete": True,
            "invoice_credits": {
                "complete": True,
                "credits": [
                    {"id": "15840539", "number": "CM11788", "invoice_id": "15805942", "total": "4.82", "tax": "0"}
                ],
                "total": "4.82",
                "tax": "0",
            },
        }
    },
}

SOURCE = {
    "number": "R000227174",
    "state": "complete",
    "completed_at": "2026-04-21T22:08:12Z",
    "customer_type": "consumer",
    "payment_state": "paid",
    "shipment_state": "shipped",
    "item_total": "2579.0",
    "ship_total": "0.0",
    "included_tax_total": "0.0",
    "additional_tax_total": "195.66",
    "adjustment_total": "190.84",
    "total": "2769.84",
    "payment_total": "2769.84",
    "adjustments": [
        {
            "label": "Fix Order Status",
            "amount": "-4.82",
            "adjustable_type": "Spree::Order",
            "source_type": None,
            "created_at": "2026-09-09T15:45:33Z",
        },
        {"label": "Sales tax", "amount": "195.66", "adjustable_type": "Spree::Order", "source_type": "Spree::TaxRate"},
    ],
    "payments": [{"amount": "100.0", "state": "completed"}, {"amount": "2669.84", "state": "completed"}],
    "line_items": [
        {
            "id": "60702230",
            "quantity": "1",
            "price": "1181.0",
            "total": "1270.59",
            "adjustments": [],
            "variant": {"name": "Ryzen AI 7 350", "sku": "FRANWD0007"},
        },
        {
            "id": "60702231",
            "quantity": "1",
            "price": "810.0",
            "total": "871.45",
            "adjustments": [],
            "variant": {"name": "DDR5-5600 - 64GB (2 x 32GB)", "sku": "FRANRM0003X2"},
        },
    ],
}

OBSERVATION = {
    "audit_id": "5d0c3d2e-0000-4000-8000-000000000001",
    "observed_at": "2026-09-29T17:20:49+00:00",
    "evidence": {
        "sections": {
            "sales_order": {"id": "14388301", "tranId": "SO1234567", "status": {"refName": "Billed"}},
            "posting_documents": [
                {
                    "id": "15805942",
                    "tranId": "INV363632",
                    "status": {"refName": "Paid In Full"},
                    "total": "2774.66",
                    "taxTotal": "195.66",
                    "amountPaid": "2769.84",
                    "amountRemaining": "0.00",
                    "createdFrom": {"id": "14388301", "refName": "Sales Order #SO1234567"},
                    "postingPeriod": {"refName": "Sep 2026"},
                    "record_type": "invoice",
                },
            ],
            "linked_documents": {
                "complete": True,
                "rows": [
                    {"id": "15805942", "type": "CustInvc", "tranid": "INV363632", "status_name": "Paid In Full"},
                    {"id": "14500001", "type": "CustDep", "tranid": "CD410934", "status_name": "Fully Applied"},
                ],
            },
            "gl": {"15805942": {"complete": True, "rows": [{"account": "1200", "debit": "2774.66"}] * 40}},
        },
        "assessment": {"root_cause": "source_order_adjustment_already_credited"},
        "blockers": [],
    },
}


def build(**changes):
    args = {
        "case": deepcopy(CASE),
        "report": deepcopy(REPORT),
        "source_order": deepcopy(SOURCE),
        "observation": deepcopy(OBSERVATION),
        "corrections": [],
    }
    args.update(changes)
    return case_file.build_case_file(**args)


def test_the_comparison_reads_as_solidus_netsuite_and_difference():
    result = build()
    assert result["case"]["order"] == "R000227174"
    order_total = result["comparison"]["metrics"]["order_total"]
    assert order_total == {"solidus": "2769.84", "netsuite": "2774.66", "difference": "-4.82", "differs": True}
    assert result["comparison"]["metrics"]["tax"]["differs"] is False
    assert result["comparison"]["currency"] == "USD"


def test_a_solidus_adjustment_equal_to_the_difference_is_a_stated_fact():
    result = build()
    assert result["solidus"]["order_adjustments"] == [
        {"label": "Fix Order Status", "amount": "-4.82", "date": "2026-09-09"}
    ]
    assert result["facts"]["adjustments_equal_to_difference"] == ["Fix Order Status"]


def test_tax_rate_rows_are_not_order_adjustments():
    labels = [a["label"] for a in build()["solidus"]["order_adjustments"]]
    assert "Sales tax" not in labels


def test_line_differences_name_the_product():
    diffs = build()["netsuite"]["line_differences"]
    assert diffs == [
        {
            "product": "DDR5-5600 - 64GB (2 x 32GB)",
            "sku": "FRANRM0003X2",
            "solidus_qty": "1",
            "netsuite_qty": "2.0",
            "solidus_amount": "810.0",
            "netsuite_net": "810.0",
            "differs": ["quantity"],
        }
    ]


def test_netsuite_net_is_the_line_amount_not_the_unit_price():
    # Live 2026-10-02 (R190994976): a 2-unit line was flagged because NetSuite's net (qty x rate)
    # was compared with Solidus's unit price. Same quantity and same amount is no difference.
    source = deepcopy(SOURCE)
    source["line_items"][0].update(quantity="2", price="45.0")
    report = deepcopy(REPORT)
    report["targets"][0]["lines"][0].update(quantity="2.0", net="90.0")
    names = [d["product"] for d in build(source_order=source, report=report)["netsuite"]["line_differences"]]
    assert "Ryzen AI 7 350" not in names
    report["targets"][0]["lines"][0].update(net="80.0")
    diff = [
        d
        for d in build(source_order=source, report=report)["netsuite"]["line_differences"]
        if d["product"] == "Ryzen AI 7 350"
    ]
    assert diff == [
        {
            "product": "Ryzen AI 7 350",
            "sku": "FRANWD0007",
            "solidus_qty": "2",
            "netsuite_qty": "2.0",
            "solidus_amount": "90.0",
            "netsuite_net": "80.0",
            "differs": ["amount"],
        }
    ]


def test_netsuite_documents_merge_saved_observation_and_invoice_credits_once_each():
    docs = {d["number"]: d for d in build()["netsuite"]["documents"]}
    assert set(docs) == {"INV363632", "CD410934", "CM11788"}
    assert docs["INV363632"] == {
        "type": "invoice",
        "number": "INV363632",
        "id": "15805942",
        "status": "Paid In Full",
        "total": "2774.66",
        "tax": "195.66",
        "paid": "2769.84",
        "remaining": "0.00",
        "created_from": "Sales Order #SO1234567",
        "period": "Sep 2026",
    }
    assert docs["CM11788"]["type"] == "credit memo" and docs["CM11788"]["created_from_id"] == "15805942"
    assert docs["CD410934"]["type"] == "customer deposit"


def test_the_sales_order_links_to_netsuite():
    so = build()["netsuite"]["sales_order"]
    assert so["id"] == "14388301" and so["number"] == "SO1234567"
    assert so["link"] == "https://6738075.app.netsuite.com/app/accounting/transactions/salesord.nl?id=14388301"


def test_gl_detail_stays_in_the_saved_observation_and_is_offered_by_reference():
    result = build()
    assert "gl" not in json.dumps(result["netsuite"]["documents"])
    more = result["read_more"]["saved_observation"]
    # GL lines come back through the saved reader's "documents" section (group_investigation.read_observation).
    assert more["observation_id"] == OBSERVATION["audit_id"] and "documents" in more["sections"]


def test_missing_inputs_are_named_never_guessed():
    result = build(source_order=None, observation=None)
    assert result["solidus"] == {"available": False, "reason": "no saved Solidus order for this case"}
    assert [d["number"] for d in result["netsuite"]["documents"]] == ["CM11788"]
    assert result["read_more"]["saved_observation"] is None
    assert "chain_read" in result["read_more"]["live"]


def test_prior_verified_fixes_are_listed():
    result = build(corrections=[("creditmemo", "15840539")])
    assert result["history"]["verified_fixes"] == [{"record_type": "creditmemo", "record_id": "15840539"}]


def test_every_amount_comes_from_the_inputs():
    result = json.dumps(build())
    for amount in ("2769.84", "2774.66", "-4.82", "4.82", "195.66", "1181.0", "810.0"):
        assert amount in result
    assert "2774.66" in json.dumps(REPORT) and "-4.82" in json.dumps(SOURCE)


def test_the_file_stays_small_even_for_a_huge_order():
    source = deepcopy(SOURCE)
    source["line_items"] = [
        {
            "id": str(i),
            "quantity": "1",
            "price": "10.0",
            "total": "10.0",
            "adjustments": [],
            "variant": {"name": f"Part {i} " + "x" * 60, "sku": f"SKU{i}"},
        }
        for i in range(400)
    ]
    report = deepcopy(REPORT)
    report["targets"][0]["lines"] = [{"key": f"line:{i}", "quantity": "2.0", "net": "10.0"} for i in range(400)]
    result = build(source_order=source, report=report)
    assert len(json.dumps(result)) <= case_file.MAX_CHARS
    assert result["truncated"] is True


def test_a_reconciled_case_says_so():
    result = build(case={**CASE, "status": "reconciled"})
    assert result["case"]["status"] == "reconciled"


# --- loader: saved evidence only, tenant-scoped ---------------------------------------

from tests.test_transaction_group_breakdown import world  # noqa: E402,F401  (fixture)


async def test_open_case_reads_only_saved_evidence(world, monkeypatch):  # noqa: F811
    from app.models.audit import AuditEvent
    from app.services.transaction_ops import netsuite_reader

    def no_reads(*args, **kwargs):
        raise AssertionError("open_case must not read NetSuite")

    monkeypatch.setattr(netsuite_reader, "authenticated_reader", no_reads)
    case = await world.case("R000000174", delta="-4.82", source_total="100.00", record_id="14388301")
    await world.solidus("R000000174", adjustments=[("Fix Order Status", "-4.82")])
    world.db.add(
        AuditEvent(
            tenant_id=world.tenant.id,
            category="transaction_ops",
            action="accounting.evidence.observed",
            actor_type="system",
            resource_type="transaction_case",
            resource_id=str(case.id),
            payload={"evidence": deepcopy(OBSERVATION["evidence"])},
            timestamp=datetime.now(timezone.utc),
        )
    )
    await world.db.flush()

    result = await case_file.open_case(world.db, world.tenant.id, order_reference="R000000174")
    assert result["case"]["id"] == str(case.id)
    assert result["facts"]["adjustments_equal_to_difference"] == ["Fix Order Status"]
    assert "INV363632" in [d["number"] for d in result["netsuite"]["documents"]]
    assert result["read_more"]["saved_observation"]["observation_id"]


async def test_open_case_never_opens_another_tenants_case(world, tenant_b):  # noqa: F811
    from app.services.transaction_ops.state_service import StateError

    await world.case("R000000175")
    with pytest.raises(StateError):
        await case_file.open_case(world.db, tenant_b.id, order_reference="R000000175")
    with pytest.raises(StateError):
        await case_file.open_case(world.db, world.tenant.id, case_id=str(uuid4()))


# --- packet review round 1 (gpt-6-astra) ----------------------------------------------


def test_included_tax_is_removed_before_comparing_with_netsuite_net():
    # F1: an EU line priced 120.00 including 20.00 VAT is 100.00 net in NetSuite: no difference.
    source = deepcopy(SOURCE)
    source.update(included_tax_total="20.0", additional_tax_total="0.0")
    source["line_items"] = [
        {
            "id": "1",
            "quantity": "1",
            "price": "120.0",
            "total": "120.0",
            "variant": {"name": "Laptop", "sku": "L1"},
            "adjustments": [{"label": "VAT", "amount": "20.0", "source_type": "Spree::TaxRate", "included": True}],
        }
    ]
    report = deepcopy(REPORT)
    report["targets"][0]["lines"] = [{"key": "line:1", "quantity": "1.0", "net": "100.0"}]
    assert build(source_order=source, report=report)["netsuite"]["line_differences"] == []


def test_a_line_with_promotions_or_unattributed_included_tax_is_not_amount_compared():
    source = deepcopy(SOURCE)
    source["line_items"] = [
        {
            "id": "1",
            "quantity": "1",
            "price": "100.0",
            "total": "90.0",
            "variant": {"name": "Laptop", "sku": "L1"},
            "adjustments": [{"label": "Promo", "amount": "-10.0", "source_type": "Spree::PromotionAction"}],
        }
    ]
    report = deepcopy(REPORT)
    report["targets"][0]["lines"] = [{"key": "line:1", "quantity": "1.0", "net": "77.0"}]
    assert build(source_order=source, report=report)["netsuite"]["line_differences"] == []
    source["line_items"][0]["adjustments"] = [{"label": "VAT", "amount": "20.0", "source_type": "Spree::TaxRate"}]
    source.update(included_tax_total="20.0")
    assert build(source_order=source, report=report)["netsuite"]["line_differences"] == []


def test_documents_include_every_saved_section_the_record_links_use():
    # F2: credits and refunds a fresh read saved under related_refund_documents, deposits and
    # invoice applications are part of the chain the agent needs.
    observation = deepcopy(OBSERVATION)
    sections = observation["evidence"]["sections"]
    sections["deposits"] = [
        {"id": "14500002", "tranId": "CD452120", "record_type": "customerdeposit", "total": "2669.84"}
    ]
    sections["invoice_applications"] = {
        "documents": {"15840539": {"id": "15840539", "tranId": "CM11788", "record_type": "creditmemo", "total": "4.82"}}
    }
    sections["related_refund_documents"] = {
        "documents": [
            {"id": "71", "tranId": "CM900", "record_type": "creditmemo", "total": "10.00"},
            {"id": "72", "tranId": "RF900", "record_type": "customerrefund", "total": "10.00"},
        ]
    }
    report = deepcopy(REPORT)
    report["refund_evidence"]["target"].pop("invoice_credits")
    docs = {d["number"]: d for d in build(observation=observation, report=report)["netsuite"]["documents"]}
    assert {"CD452120", "CM11788", "CM900", "RF900"} <= set(docs)
    assert docs["RF900"]["type"] == "customer refund" and docs["CM11788"]["type"] == "credit memo"


def test_the_size_limit_holds_for_any_input():
    # F3: twenty matching adjustments with long labels once produced 53k characters.
    source = deepcopy(SOURCE)
    source["adjustments"] = [
        {"label": f"Adjustment {i} " + "y" * 2000, "amount": "-4.82", "adjustable_type": "Spree::Order"}
        for i in range(20)
    ]
    result = build(source_order=source, corrections=[("creditmemo", str(i)) for i in range(200)])
    assert len(json.dumps(result)) <= case_file.MAX_CHARS
    assert result["truncated"] is True


def test_any_cut_list_marks_the_file_truncated():
    # F4: 21 adjustments and 11 payments were cut to 20 and 10 with truncated=false.
    source = deepcopy(SOURCE)
    source["adjustments"] = [
        {"label": f"A{i}", "amount": "-1.00", "adjustable_type": "Spree::Order"} for i in range(21)
    ]
    source["payments"] = [{"amount": "1.00", "state": "completed"} for _ in range(11)]
    assert build(source_order=source)["truncated"] is True


def test_read_more_names_only_sections_the_saved_reader_accepts():
    # F5: the saved-observation reader accepts source, documents, applications and assessment only.
    more = build()["read_more"]["saved_observation"]
    assert set(more["sections"]) <= {"source", "documents", "applications", "assessment"}


# --- packet review round 2: the two shapes made unrepresentable ------------------------


@pytest.mark.parametrize(
    "order_tax, line_changes",
    [
        ("20.0", {"adjustments": []}),  # included tax on the order, nothing attributed to the line
        ("0.0", {"adjustments": [{"label": "VAT", "amount": None, "source_type": "Spree::TaxRate", "included": True}]}),
        ("0.0", {"included_tax_total": "20.0", "adjustments": []}),
    ],
)
def test_any_included_tax_means_the_amount_is_not_compared(order_tax, line_changes):
    # Round 2 F1: amounts are compared only when nothing is included in the price.
    source = deepcopy(SOURCE)
    source.update(included_tax_total=order_tax)
    source["line_items"] = [
        {
            "id": "1",
            "quantity": "1",
            "price": "120.0",
            "total": "120.0",
            "variant": {"name": "Laptop", "sku": "L1"},
            **line_changes,
        }
    ]
    report = deepcopy(REPORT)
    report["targets"][0]["lines"] = [{"key": "line:1", "quantity": "1.0", "net": "100.0"}]
    result = build(source_order=source, report=report)
    assert result["netsuite"]["line_differences"] == []
    assert result["solidus"]["lines"][0]["amount_not_compared"]


def test_the_bound_holds_when_every_field_is_huge():
    # Round 2 F3: 400 comparison adjustments, and a 2,048-character currency, each broke the bound.
    report = deepcopy(REPORT)
    report["balance"]["adjustments"] = [{"kind": f"kind-{i}-" + "z" * 50, "total": "1.00"} for i in range(400)]
    report["balance"]["currency"] = "\U0001f4b0" * 2048
    report["balance"]["reason"] = "r" * 5000
    result = build(report=report, case={**CASE, "order_reference": "R" * 3000})
    assert len(json.dumps(result)) <= case_file.MAX_CHARS
    assert result["truncated"] is True
