import re
import sqlite3

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
        # Execute the actual ownership SQL against representative native tables;
        # only rename SQLite's reserved TRANSACTION table name. This catches
        # incorrect joins and filtering after the provider's page boundary.
        with sqlite3.connect(":memory:") as db:
            db.row_factory = sqlite3.Row
            db.executescript("""
                CREATE TABLE native_transaction (id TEXT PRIMARY KEY, type TEXT,
                    custbody_fw_order_number TEXT);
                CREATE TABLE transactionline ("transaction" TEXT, mainline TEXT, subsidiary TEXT);
                CREATE TABLE NextTransactionLink (previousdoc TEXT, nextdoc TEXT);
                CREATE TABLE customrecord_fw_refund_requests (id TEXT,
                    custrecord_refreq_so_link TEXT, custrecord_refreq_order_number TEXT,
                    custrecord_refreq_cm_link TEXT, custrecord_refreq_refund_link TEXT,
                    custrecord_refreq_cust_dep_link TEXT);
            """)
            for row in self.records:
                db.execute(
                    "INSERT OR IGNORE INTO native_transaction VALUES (?,?,?)",
                    (row["id"], row["type"], row["order_reference"]),
                )
                db.execute("INSERT INTO transactionline VALUES (?,'T',?)", (row["id"], row["subsidiary"]))
            for row in self.edges:
                # Native edge endpoints exist, even when they are irrelevant to
                # order ownership (e.g. inventory adjustment -> shipment).
                for prefix in ("previous", "next"):
                    db.execute(
                        "INSERT OR IGNORE INTO native_transaction VALUES (?,?,NULL)",
                        (row[prefix + "doc"], row[prefix + "type"]),
                    )
                db.execute("INSERT INTO NextTransactionLink VALUES (?,?)", (row["previousdoc"], row["nextdoc"]))
            for row in self.requests:
                db.execute(
                    "INSERT INTO customrecord_fw_refund_requests VALUES (?,?,?,? ,?,?)",
                    (
                        row["id"],
                        row.get("order_id"),
                        row.get("order_reference"),
                        row.get("credit_id", "3"),
                        row.get("refund_id", "4"),
                        row.get("deposit_id"),
                    ),
                )
            sql = re.sub(r"(FROM|JOIN) transaction ", r"\1 native_transaction ", sql)
            sql = sql.replace("m.transaction", 'm."transaction"')
            rows = [dict(row) for row in db.execute(sql)]
        total = len(rows)
        rows = rows[params["offset"] : params["offset"] + params["limit"]]
        return {"items": rows, "count": len(rows), "totalResults": total, "hasMore": self.partial or total > len(rows)}


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


async def test_inventory_fanout_is_filtered_before_provider_page_limit():
    reader = Reader()
    reader.records += [record(7, "InvAdjst"), record(8, "ItemRcpt")]
    reader.edges += [edge(7, i, "InvAdjst", "ItemShip") for i in range(100, 3953)]
    reader.edges += [edge(8, 4, "ItemRcpt", "CustRfnd")]
    result = await owners(reader, ("4", "7", "8"))
    assert result["order_references"] == ["R000000001"]
    assert len(reader.calls) == 6


async def test_multisubsidiary_journal_does_not_duplicate_native_identity():
    reader = Reader()
    reader.records += [record(7, "Journal", "1"), record(7, "Journal", "2")]
    reader.edges += [edge(7, 3, "Journal", "DepAppl")]
    result = await owners(reader, ("4", "7"))
    assert result == {"order_references": ["R000000001"], "outside_subsidiary_ids": []}


async def test_ambiguous_sales_order_subsidiary_still_fails_closed():
    reader = Reader()
    reader.records.append(record(1, "SalesOrd", "3"))
    with pytest.raises(NetSuiteEvidenceError, match="dependency_owner_identity_unproven"):
        await owners(reader)


async def test_missing_sales_order_subsidiary_still_fails_closed():
    reader = Reader()
    reader.records[0]["subsidiary"] = None
    with pytest.raises(NetSuiteEvidenceError, match="dependency_owner_identity_unproven"):
        await owners(reader)


async def test_traversable_graph_over_limit_still_cannot_return_partial_owners():
    reader = Reader()
    reader.edges += [edge(i, 4, "CustCred", "CustRfnd") for i in range(100, 301)]
    with pytest.raises(NetSuiteEvidenceError, match="dependency_owner_page_incomplete"):
        await owners(reader)
