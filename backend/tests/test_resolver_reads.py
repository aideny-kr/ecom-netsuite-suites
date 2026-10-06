"""Resolver read tools (spec 2026-10-01 §5, block B6): the live document chain, bounded SuiteQL, schema."""

from __future__ import annotations

import re

import pytest

from app.services.transaction_ops import resolver_reads as reads
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError

COLUMNS = ("id", "type", "tranid", "status", "foreigntotal", "taxtotal", "trandate", "memo", "createdfrom")


def row(id, type, tranid, createdfrom=None, total=None, status=None):
    return {
        "id": id,
        "type": type,
        "tranid": tranid,
        "status": status,
        "foreigntotal": total,
        "taxtotal": None,
        "trandate": "09/25/2026",
        "memo": None,
        "createdfrom": createdfrom,
    }


# An order with a deposit, a fulfillment and an invoice; the invoice has a credit memo.
WORLD = [
    row(100, "SalesOrd", "SO100", total=5253),
    row(201, "CustDep", "CD201", 100, 4990.36),
    row(202, "ItemShip", "IF202", 100),
    row(203, "CustInvc", "INV203", 100, 5253),
    row(301, "CustCred", "CM301", 203, -262.64),
]


class FakeReader:
    """Answers the chain's SuiteQL from WORLD, the way the connector does, and records every call."""

    def __init__(self, world=WORLD, *, has_more=False, error=None, children_more=False):
        self.world, self.has_more, self.error, self.children_more = world, has_more, error, children_more
        self.calls = []

    async def request(self, method, path, *, params=None, body=None):
        self.calls.append((method, path, params, body))
        if self.error:
            raise NetSuiteEvidenceError(self.error)
        if path.startswith("/record/v1/metadata-catalog/"):
            return {"properties": {"entity": {"title": "Customer", "type": "object"}, "memo": {"type": "string"}}}
        sql = body["q"]
        by_id = re.search(r"t\.id IN \(([\d,]+)\)", sql)
        by_parent = re.search(r"tl\.createdfrom IN \(([\d,]+)\)", sql)
        if by_id:
            wanted = {int(x) for x in by_id.group(1).split(",")}
            items = [r for r in self.world if r["id"] in wanted]
        elif by_parent:
            wanted = {int(x) for x in by_parent.group(1).split(",")}
            items = [r for r in self.world if r["createdfrom"] in wanted]
        else:
            items = [{"n": 1}]
        more = self.has_more or (self.children_more and by_parent is not None)
        return {"items": items, "count": len(items), "totalResults": len(items), "hasMore": more, "links": []}


def _numbers(result):
    return [d["number"] for d in result["documents"]]


async def test_the_chain_from_an_order_lists_its_documents_and_their_credits():
    result = await reads.chain_read(FakeReader(), "100")
    assert _numbers(result) == ["SO100", "CD201", "IF202", "INV203", "CM301"]
    by = {d["number"]: d for d in result["documents"]}
    assert by["CM301"]["type"] == "credit memo" and by["CM301"]["created_from"] == "203" and by["CM301"]["depth"] == 2
    assert by["SO100"]["depth"] == 0 and result["top"] == "100" and result["complete"] is True


async def test_the_chain_from_a_credit_memo_walks_up_to_the_order_first():
    result = await reads.chain_read(FakeReader(), "301")
    assert result["root"] == "301" and result["top"] == "100"
    assert _numbers(result) == ["SO100", "CD201", "IF202", "INV203", "CM301"]


async def test_every_query_is_bounded_by_ids_never_an_open_join():
    reader = FakeReader()
    await reads.chain_read(reader, "301")
    sqls = [c[3]["q"] for c in reader.calls]
    assert all(re.search(r"(t\.id|tl\.createdfrom) IN \(\d+(,\d+)*\)", q) for q in sqls)
    assert all(c[2]["limit"] <= reads.MAX_CHAIN_DOCUMENTS for c in reader.calls)


@pytest.mark.parametrize("bad", ["100; DROP", "1 OR 1=1", "", "abc", None, "-1"])
async def test_a_record_id_must_be_a_netsuite_internal_id(bad):
    reader = FakeReader()
    with pytest.raises(ValueError):
        await reads.chain_read(reader, bad)
    assert reader.calls == []


async def test_an_unknown_record_is_named_not_guessed():
    assert (await reads.chain_read(FakeReader(), "999"))["error"] == "record_not_found"


async def test_an_incomplete_page_marks_the_chain_incomplete():
    assert (await reads.chain_read(FakeReader(has_more=True), "100"))["complete"] is False
    # Only a page of children cut short: the root and parents read whole.
    assert (await reads.chain_read(FakeReader(children_more=True), "301"))["complete"] is False


async def test_the_chain_is_capped_and_says_so():
    world = [row(100, "SalesOrd", "SO100")] + [row(1000 + i, "CustDep", f"CD{i}", 100) for i in range(60)]
    result = await reads.chain_read(FakeReader(world), "100")
    assert len(result["documents"]) == reads.MAX_CHAIN_DOCUMENTS and result["complete"] is False


async def test_a_created_from_loop_ends_without_reading_a_document_twice():
    world = [row(1, "CustInvc", "A", 2), row(2, "CustInvc", "B", 1)]
    reader = FakeReader(world)
    result = await reads.chain_read(reader, "1")
    assert sorted(_numbers(result)) == ["A", "B"]
    up = [c[3]["q"] for c in reader.calls if "t.id IN" in c[3]["q"]]
    assert len(up) == len(set(up)) == 2


async def test_a_connector_failure_is_returned_as_an_error():
    assert (await reads.chain_read(FakeReader(error="upstream_http_429"), "100"))["error"] == "upstream_http_429"


# --- netsuite_query -------------------------------------------------------------------------


async def test_a_query_runs_as_written_with_a_row_bound():
    reader = FakeReader()
    result = await reads.netsuite_query(reader, "SELECT 1 AS n FROM dual", limit=10)
    assert reader.calls == [("POST", "/query/v1/suiteql", {"limit": 10, "offset": 0}, {"q": "SELECT 1 AS n FROM dual"})]
    assert result == {"rows": [{"n": 1}], "row_count": 1, "complete": True}


async def test_a_query_with_more_rows_than_the_bound_says_it_is_incomplete():
    assert (await reads.netsuite_query(FakeReader(has_more=True), "SELECT 1 FROM dual"))["complete"] is False


@pytest.mark.parametrize("sql, limit", [("", 10), ("   ", 10), (None, 10), ("SELECT 1", 0), ("SELECT 1", 1001)])
async def test_a_query_needs_text_and_a_sane_bound(sql, limit):
    with pytest.raises(ValueError):
        await reads.netsuite_query(FakeReader(), sql, limit=limit)


async def test_a_rejected_query_returns_the_connector_error():
    assert (await reads.netsuite_query(FakeReader(error="invalid_search_query"), "SELECT x FROM y"))["error"] == (
        "invalid_search_query"
    )


# --- netsuite_schema ------------------------------------------------------------------------


async def test_a_schema_is_read_on_demand_and_says_requirements_are_unknown():
    reader = FakeReader()
    result = await reads.netsuite_schema(reader, "creditMemo")
    assert reader.calls[0][:2] == ("GET", "/record/v1/metadata-catalog/creditMemo")
    assert {f["name"] for f in result["fields"]} >= {"entity", "memo"}
    assert result["requirements_known"] is False  # the live catalog carries no requiredness


@pytest.mark.parametrize("bad", ["../secrets", "credit memo", "", "creditMemo?x=1", None])
async def test_a_schema_name_must_be_a_record_type_name(bad):
    reader = FakeReader()
    with pytest.raises(ValueError):
        await reads.netsuite_schema(reader, bad)
    assert reader.calls == []
