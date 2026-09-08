"""Custom refund-request ownership, using a sanitized standalone VAT-credit shape."""

from copy import deepcopy
from decimal import Decimal

import pytest

from app.services.transaction_ops.netsuite_refunds import collect_refunds
from tests.test_netsuite_refund_graph import Reader, edge

REFERENCE = "R123456789"


class CustomReader(Reader):
    def __init__(self):
        super().__init__()
        self.edges = [edge("4", "3", "CustRfnd", "CustCred")]
        self.record["total"] = "578.38"
        self.record["apply"]["items"] = [{"apply": True, "doc": {"id": "3"}, "line": 0, "amount": "578.38"}]
        self.requests = [
            {
                "id": "20",
                "name": "30",
                "order_reference": REFERENCE,
                "order_id": "1",
                "processed": "T",
                "credit_id": "3",
                "refund_id": "4",
                "amount": "578.38",
                "payment_number": "H4T7UYLR",
                "reason_id": "102",
                "credit_type": "CustCred",
                "credit_currency": "1",
                "credit_subsidiary": "1",
                "credit_posting": "T",
                "credit_voided": "F",
                "credit_reference": REFERENCE,
            }
        ]
        self.conflicts = None
        self.request_complete = True
        self.reverse_complete = True

    async def request(self, method, path, **kwargs):
        query = kwargs.get("body", {}).get("q", "")
        if "customrecord_fw_refund_requests" in query:
            self.calls += 1
            reverse = "r.custrecord_refreq_cm_link IN (" in query
            rows = self.conflicts if reverse and self.conflicts is not None else self.requests
            complete = self.reverse_complete if reverse else self.request_complete
            return {"items": deepcopy(rows), "count": len(rows), "totalResults": len(rows), "hasMore": not complete}
        return await super().request(method, path, **kwargs)


async def test_standalone_vat_credit_request_proves_refund_without_standard_order_edge():
    result = await collect_refunds(CustomReader(), "1", "1", "1", order_reference=REFERENCE)
    assert result["amount"] == Decimal("578.38")
    assert result["record_ids"] == ["4"] and result["refund_count"] == 1
    link = result["request_links"][0]
    assert link == {
        "request_id": "20",
        "source_refund_id": "30",
        "payment_number": "H4T7UYLR",
        "credit_memo_id": "3",
        "refund_id": "4",
        "amount": "578.38",
        "reason_id": "102",
        "stage": "refund_verified",
    }


async def test_standard_and_custom_paths_do_not_double_count_the_same_refund():
    reader = CustomReader()
    reader.edges.insert(0, edge("1", "3", "SalesOrd", "CustCred"))
    result = await collect_refunds(reader, "1", "1", "1", order_reference=REFERENCE)
    assert result["amount"] == Decimal("578.38") and result["refund_count"] == 1


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_order",
        "wrong_order_id",
        "wrong_credit_order",
        "wrong_currency",
        "wrong_subsidiary",
        "wrong_type",
        "unposted_credit",
        "voided_credit",
        "partial_requests",
        "partial_reverse",
        "duplicate_request",
        "shared_credit",
        "shared_standard_parent",
        "wrong_refund_link",
        "amount_disagrees",
        "missing_native_application",
        "changed_request",
    ],
)
async def test_custom_links_never_bypass_identity_completeness_or_allocation_checks(failure):
    reader = CustomReader()
    row = reader.requests[0]
    changes = {
        "wrong_order": ("order_reference", "R999999999"),
        "wrong_order_id": ("order_id", "900"),
        "wrong_credit_order": ("credit_reference", "R999999999"),
        "wrong_currency": ("credit_currency", "2"),
        "wrong_subsidiary": ("credit_subsidiary", "2"),
        "wrong_type": ("credit_type", "CustInvc"),
        "unposted_credit": ("credit_posting", "F"),
        "voided_credit": ("credit_voided", "T"),
        "wrong_refund_link": ("refund_id", "900"),
        "amount_disagrees": ("amount", "578.39"),
    }
    if failure in changes:
        key, value = changes[failure]
        row[key] = value
    elif failure == "partial_requests":
        reader.request_complete = False
    elif failure == "partial_reverse":
        reader.reverse_complete = False
    elif failure == "duplicate_request":
        reader.requests.append(deepcopy(row))
    elif failure == "shared_credit":
        other = {**row, "id": "21", "name": "31", "order_id": "900", "order_reference": "R999999999"}
        reader.conflicts = [row, other]
    elif failure == "shared_standard_parent":
        reader.edges.append(edge("900", "3", "SalesOrd", "CustCred"))
    elif failure == "missing_native_application":
        reader.edges = []
    else:
        reader.conflicts = [{**row, "refund_id": "901"}]
    with pytest.raises(ValueError):
        await collect_refunds(reader, "1", "1", "1", order_reference=REFERENCE)


async def test_credit_without_customer_refund_is_not_returned_money():
    reader = CustomReader()
    reader.requests[0].pop("refund_id")
    reader.edges = []
    result = await collect_refunds(reader, "1", "1", "1", order_reference=REFERENCE)
    assert result["amount"] == 0 and result["refund_count"] == 0
    assert result["request_links"][0]["stage"] == "credit_only"


async def test_unprocessed_request_cannot_establish_completed_refund():
    reader = CustomReader()
    reader.requests = [{"id": "20", "name": "30", "order_reference": REFERENCE, "processed": "F", "amount": "578.38"}]
    reader.edges = []
    result = await collect_refunds(reader, "1", "1", "1", order_reference=REFERENCE)
    assert result["amount"] == 0
    assert result["request_links"][0]["stage"] == "pending"


@pytest.mark.parametrize("reference", ["R123456789' OR 1=1", "", None])
async def test_invalid_order_reference_is_rejected_before_network_reads(reference):
    reader = CustomReader()
    with pytest.raises(ValueError):
        await collect_refunds(reader, "1", "1", "1", order_reference=reference)
    assert reader.calls == 0


async def test_changed_custom_link_during_native_refund_read_is_not_certified():
    reader = CustomReader()
    original = reader.request

    async def request(method, path, **kwargs):
        result = await original(method, path, **kwargs)
        if method == "GET":
            reader.requests[0]["refund_id"] = "901"
        return result

    reader.request = request
    with pytest.raises(ValueError, match="shared_or_changed"):
        await collect_refunds(reader, "1", "1", "1", order_reference=REFERENCE)


async def test_two_custom_records_cannot_claim_the_same_source_refund_identity():
    reader = CustomReader()
    reader.requests = [
        {"id": rid, "name": "30", "order_reference": REFERENCE, "processed": "F", "amount": "578.38"}
        for rid in ["20", "21"]
    ]
    reader.edges = []
    with pytest.raises(ValueError, match="identity_unproven"):
        await collect_refunds(reader, "1", "1", "1", order_reference=REFERENCE)


async def test_custom_reader_preserves_the_existing_native_call_budget():
    reader = CustomReader()
    reader.edges += [edge(str(i), "3", "CustRfnd", "CustCred") for i in range(10, 45)]
    original = reader.request

    async def request(method, path, **kwargs):
        result = await original(method, path, **kwargs)
        if method == "GET":
            result["id"] = path.rsplit("/", 1)[-1]
        return result

    reader.request = request
    with pytest.raises(ValueError, match="budget"):
        await collect_refunds(reader, "1", "1", "1", order_reference=REFERENCE)
    assert reader.calls <= 24


async def test_no_custom_record_permission_never_falls_back_to_proving_zero():
    reader = CustomReader()

    async def request(*args, **kwargs):
        raise ValueError("native_permission_denied")

    reader.request = request
    with pytest.raises(ValueError, match="native_permission_denied"):
        await collect_refunds(reader, "1", "1", "1", order_reference=REFERENCE)
