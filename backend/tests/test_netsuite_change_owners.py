import re

import pytest

from app.services.transaction_ops.netsuite_change_owners import collect_order_candidates
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError


def record(identifier, kind, subsidiary="2"):
    return {
        "id": str(identifier),
        "type": kind,
        "subsidiary": subsidiary,
        "order_reference": f"R{identifier:09d}" if kind == "SalesOrd" else None,
    }


def edge(previous, following, before, after):
    return {"previousdoc": str(previous), "nextdoc": str(following), "previoustype": before, "nexttype": after}


class Reader:
    def __init__(self):
        self.records = [record(1, "SalesOrd"), record(2, "CustDep"), record(3, "DepAppl"), record(4, "CustRfnd")]
        self.edges = [
            edge(1, 2, "SalesOrd", "CustDep"),
            edge(2, 3, "CustDep", "DepAppl"),
            edge(4, 3, "CustRfnd", "DepAppl"),
        ]
        self.requests = []
        self.calls = []
        self.partial = False

    async def request(self, method, path, *, params, body):
        assert method == "POST" and path == "/query/v1/suiteql" and params["limit"] == 201
        sql = body["q"]
        self.calls.append(sql)
        if "FROM NextTransactionLink" in sql:
            ids = set(re.search(r"l.previousdoc IN \(([^)]+)\)", sql)[1].split(","))
            rows = [r for r in self.edges if r["previousdoc"] in ids or r["nextdoc"] in ids]
        elif "FROM customrecord_fw_refund_requests" in sql:
            rows = self.requests
        else:
            match = re.search(r"t.id IN \(([^)]+)\)", sql)
            ids = set(match[1].split(",")) if match else set()
            refs = set(re.findall(r"'R[0-9]{9}'", sql))
            rows = [r for r in self.records if r["id"] in ids or "'" + str(r["order_reference"]) + "'" in refs]
        return {"items": rows, "count": len(rows), "totalResults": len(rows), "hasMore": self.partial}


async def owners(reader, documents=("4",), **kwargs):
    return await collect_order_candidates(
        reader.request,
        "2",
        "custbody_fw_order_number",
        documents,
        kwargs.get("order_ids", ()),
        kwargs.get("references", ()),
    )


async def test_reverse_refund_application_finds_its_sales_order():
    reader = Reader()
    result = await owners(reader)
    assert result == {"order_references": ["R000000001"], "outside_subsidiary_ids": []}
    assert len(reader.calls) == 6


async def test_shared_document_nominates_every_in_scope_order_without_assigning_money():
    reader = Reader()
    reader.records = [record(1, "SalesOrd"), record(5, "SalesOrd"), record(6, "SalesOrd", "3"), record(3, "CustCred")]
    reader.edges = [edge(i, 3, "SalesOrd", "CustCred") for i in (1, 5, 6)]
    result = await owners(reader, ("3",))
    assert result == {"order_references": ["R000000001", "R000000005"], "outside_subsidiary_ids": ["6"]}
    assert "amount" not in result


async def test_custom_request_resolves_standalone_credit_and_filters_other_entity():
    reader = Reader()
    reader.records = [record(1, "SalesOrd"), record(5, "SalesOrd", "3"), record(3, "CustCred")]
    reader.edges = []
    reader.requests = [
        {"id": "20", "order_id": "1", "order_reference": "R000000001"},
        {"id": "21", "order_id": "5", "order_reference": "R000000005"},
    ]
    assert (await owners(reader, ("3",)))["order_references"] == ["R000000001"]


async def test_custom_ownership_on_an_ancestor_of_the_changed_refund_is_included():
    reader = Reader()
    reader.records = [record(1, "SalesOrd"), record(3, "CustCred"), record(4, "CustRfnd")]
    reader.edges = [edge(3, 4, "CustCred", "CustRfnd")]
    reader.requests = [{"id": "20", "order_id": "1", "order_reference": "R000000001"}]
    assert (await owners(reader))["order_references"] == ["R000000001"]
    custom_query = next(sql for sql in reader.calls if "FROM customrecord_fw_refund_requests" in sql)
    assert "custrecord_refreq_cm_link IN (3,4)" in custom_query


async def test_unlinked_request_reference_is_resolved_against_native_entity():
    reader = Reader()
    reader.records.append(record(5, "SalesOrd", "3"))
    result = await owners(reader, (), references=("R000000001", "R000000005"))
    assert result == {"order_references": ["R000000001"], "outside_subsidiary_ids": ["5"]}
    assert len(reader.calls) == 1


async def test_deleted_or_hidden_record_is_not_recreated_by_owner_lookup():
    reader = Reader()
    assert (await owners(reader, ("999",)))["order_references"] == []
    assert len(reader.calls) == 2  # Native identity + remaining custom links only.


async def test_truncated_graph_or_shared_identity_never_yields_a_partial_owner_list():
    reader = Reader()
    reader.partial = True
    with pytest.raises(NetSuiteEvidenceError, match="dependency_owner_page_incomplete"):
        await owners(reader)


@pytest.mark.parametrize(
    "documents,references",
    [((True,), ()), (("1 OR 1=1",), ()), (("4",), ("R000000001' OR 1=1",)), (tuple(str(i) for i in range(1, 102)), ())],
)
async def test_invalid_scope_cannot_enter_native_queries(documents, references):
    reader = Reader()
    with pytest.raises(NetSuiteEvidenceError, match="dependency_owner_scope_invalid"):
        await owners(reader, documents, references=references)
    assert reader.calls == []
