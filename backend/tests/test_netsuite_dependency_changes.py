import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.transaction_ops import netsuite_dependency_changes as reader

START = datetime(2026, 9, 6, tzinfo=timezone.utc)
END = START + timedelta(days=1)


def row(identifier=1, **fields):
    return {
        "id": str(identifier),
        "type": "CustRfnd",
        "subsidiary": "2",
        "order_reference": None,
        "modified_utc": "2026-09-06T18:21:37Z",
        **fields,
    }


def response(rows, more=False):
    return {"items": rows, "count": len(rows), "totalResults": len(rows) + int(more), "hasMore": more, "offset": 0}


@pytest.fixture
def transport(monkeypatch):
    request = AsyncMock(return_value=response([row()]))

    @asynccontextmanager
    async def auth(*args, **kwargs):
        assert kwargs["max_api_calls"] == 1
        yield type("Reader", (), {"request": request})()

    monkeypatch.setattr(reader, "authenticated_reader", auth)
    return request


async def read(stream="transactions", **kwargs):
    return await reader.read_change_page(
        None,
        uuid4(),
        uuid4(),
        "6738075",
        "2",
        "custbody_fw_order_number",
        stream,
        START,
        END,
        **kwargs,
    )


@pytest.mark.parametrize("stream", ["transaction_lines", "transaction_links"])
def test_day_membership_keeps_earlier_changes_without_changing_group_grain(monkeypatch, stream):
    import re

    monkeypatch.setattr(reader, "_utc", lambda column: column)
    monkeypatch.setattr(reader, "_stamp", lambda expression: f"{expression} AS modified_utc")
    monkeypatch.setattr(reader, "_dates", lambda column, lower, upper: f"{column}>='{lower}' AND {column}<'{upper}'")
    finish = END + timedelta(days=1)
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE native_transaction (id INTEGER, type TEXT, ref TEXT);
            CREATE TABLE transactionline ("transaction" INTEGER, mainline TEXT, subsidiary INTEGER, linelastmodifieddate TEXT);
            CREATE TABLE NextTransactionLineLink (previousdoc INTEGER, nextdoc INTEGER, lastmodifieddate TEXT);
            INSERT INTO native_transaction VALUES (10,'SalesOrd','R000000010'),(20,'SalesOrd','R000000020');
            INSERT INTO transactionline VALUES (10,'T',2,'2026-01-01'),(20,'T',2,'2026-01-01');
        """)
        for identifier, stamp in [
            (10, "2026-09-06 18:00:00.000000"),
            (10, "2026-09-07 18:00:00.000000"),
            (20, "2026-09-07 19:00:00.000000"),
        ]:
            db.execute("INSERT INTO transactionline VALUES (?,'F',2,?)", (identifier, stamp))
            db.execute("INSERT INTO NextTransactionLineLink VALUES (?,99,?)", (identifier, stamp))
        queries = [
            reader.change_query(
                stream,
                "2",
                "ref",
                START,
                finish,
                [0, 0] if stream == "transaction_links" else [0],
                membership_windows=windows,
            )
            for windows in [(), [(START, END), (END, finish)]]
        ]
        results = []
        for sql in queries:
            sql = sql.replace("JOIN transaction t", "JOIN native_transaction t")
            sql = re.sub(r"\b([lm])\.transaction\b", r'\1."transaction"', sql)
            sql = re.sub(r"TO_TIMESTAMP\('([^']+)','YYYY-MM-DD HH24:MI:SS.FF6'\)", r"'\1'", sql)
            results.append(db.execute(sql).fetchall())
    width = len(results[0][0])
    assert [row[:width] for row in results[1]] == results[0]
    assert [row[width:] for row in results[1]] == [(1, 1), (0, 1)]


async def test_membership_flags_are_optional_proof_and_never_guessed(transport):
    transport.return_value = response([row(membership_0="1")])
    page = await read("transaction_lines", membership_windows=[(START, END)])
    assert page["changes"][0]["membership_windows"] == [[START.isoformat(), END.isoformat()]]
    transport.return_value = response([row()])
    page = await read("transaction_lines", membership_windows=[(START, END)])
    assert page["changes"][0]["membership_windows"] is None
    assert page["page_complete"]  # Candidate extraction still has its original proof.


@pytest.mark.parametrize("stream", reader.STREAMS)
async def test_query_exhaustion_is_only_candidate_inventory(transport, stream):
    transport.return_value = response([])
    page = await read(stream)
    assert page["scan_complete"] and page["next_cursor"] is None and page["changes"] == []
    assert page["evidence_use"] == "candidate_invalidation_only"
    assert "fresh" not in page and "financial_status" not in page
    assert transport.call_count == 1
    assert transport.call_args.kwargs["params"] == {"limit": 21, "offset": 0}
    query = transport.call_args.kwargs["body"]["q"]
    if stream == "deletions":
        assert "SYS_EXTRACT_UTC" not in query
        assert " AS del_date " in query
        assert "TO_DATE('2026-09-06 00:00:00','YYYY-MM-DD HH24:MI:SS')-2" in query
        assert page["window_semantics"] == "conservative_date_envelope"
    else:
        assert "TO_TIMESTAMP('2026-09-06 00:00:00.000000'" in query
        assert "TO_TIMESTAMP('2026-09-07 00:00:00.000000'" in query
        assert "SYS_EXTRACT_UTC" in query
    assert "ORDER BY" in query


async def test_link_cursor_pages_two_endpoint_identities_without_losing_shared_previous_doc(transport):
    transport.return_value = response(
        [
            row(previousdoc="10", nextdoc="11"),
            row(previousdoc="10", nextdoc="12"),
            row(previousdoc="11", nextdoc="9"),
        ],
        True,
    )
    page = await read("transaction_links", after=[10, 10], page_size=2)
    assert page["next_cursor"] == [10, 12] and not page["scan_complete"]
    assert page["changes"][1]["record_keys"] == [("transaction", "10"), ("transaction", "12")]
    query = transport.call_args.kwargs["body"]["q"]
    assert "l.previousdoc>10 OR (l.previousdoc=10 AND l.nextdoc>10)" in query
    assert "m.subsidiary=2" in query


async def test_transaction_and_line_changes_keep_parent_identity_and_subsidiary(transport):
    for stream in ("transactions", "transaction_lines"):
        transport.return_value = response([row(17)])
        page = await read(stream)
        assert page["changes"][0]["record_keys"] == [("transaction", "17")]
        assert page["changes"][0]["transaction_type"] == "CustRfnd"
        assert page["scope"]["subsidiary_id"] == "2"


@pytest.mark.parametrize("after,expected_ids", [(0, [10, 20]), (10, [20]), (20, [])])
def test_line_inventory_preserves_line_only_changes_and_parent_grain(monkeypatch, after, expected_ids):
    # Execute the generated relational query over an independent fixture.
    # Only Oracle timestamp rendering is adapted; live REST equivalence covers
    # that dialect. Old header dates must not exclude newly changed lines.
    monkeypatch.setattr(reader, "_utc", lambda column: column)
    monkeypatch.setattr(reader, "_stamp", lambda expression: f"{expression} AS modified_utc")
    monkeypatch.setattr(
        reader,
        "_dates",
        lambda column, lower, upper: f"{column}>='{lower}' AND {column}<'{upper}'",
    )
    with sqlite3.connect(":memory:") as db:
        db.executescript(
            'CREATE TABLE "transaction" (id INTEGER, type TEXT, ref TEXT, lastmodifieddate TEXT);'
            'CREATE TABLE transactionline ("transaction" INTEGER, mainline TEXT, subsidiary INTEGER, '
            "linelastmodifieddate TEXT);"
        )
        db.executemany(
            'INSERT INTO "transaction" VALUES (?,?,?,?)',
            [
                (i, kind, f"R{i}", "2026-01-01")
                for i, kind in [(10, "SalesOrd"), (20, "CustCred"), (30, "SalesOrd"), (40, "Journal"), (50, "SalesOrd")]
            ],
        )
        lo = "2026-09-06 00:00:00.000000"
        middle = "2026-09-06 18:00:00.000000"
        hi = "2026-09-07 00:00:00.000000"
        db.executemany(
            "INSERT INTO transactionline VALUES (?,?,?,?)",
            [
                (10, "T", 2, "2026-01-01"),
                (10, "T", 2, "2026-01-01"),
                (10, "F", 2, lo),
                (10, "F", 2, middle),
                (10, "F", 2, hi),
                (20, "T", 2, "2026-01-01"),
                (20, "F", 2, lo),
                (30, "T", 3, middle),
                (40, "T", 2, middle),
                (50, "T", 2, "2026-01-01"),
                (50, "F", 2, hi),
            ],
        )
        query = reader.change_query("transaction_lines", "2", "ref", START, END, [after])
        query = query.replace("l.transaction", 'l."transaction"').replace("m.transaction", 'm."transaction"')
        # The derived alias is an identifier, not the quoted source column.
        query = query.replace('l."transaction"_id', "l.transaction_id").replace(
            "JOIN transaction t", 'JOIN "transaction" t'
        )
        rows = db.execute(query).fetchall()
    assert [r[0] for r in rows] == expected_ids
    assert rows == [
        (i, "SalesOrd" if i == 10 else "CustCred", f"R{i}", 2, middle if i == 10 else lo) for i in expected_ids
    ]


async def test_deletion_identity_nominates_both_record_namespaces(transport):
    transport.return_value = response([row(17, del_date="2026-09-06T18:21:37")])
    page = await read("deletions")
    assert page["changes"][0]["record_keys"] == [
        ("transaction", "17"),
        ("customrecord_fw_refund_requests", "17"),
    ]
    assert "GROUP BY d.recordid" in transport.call_args.kwargs["body"]["q"]
    assert page["changes"][0]["modified_at"] is None


async def test_deletion_envelope_keeps_boundary_candidates_without_inventing_utc(transport):
    transport.return_value = response([row(17, del_date="2026-09-05T19:00:00")])
    page = await read("deletions")
    assert page["changes"][0]["deleted_at_raw"] == "2026-09-05T19:00:00"
    assert page["changes"][0]["modified_at"] is None


async def test_unlinked_refund_request_retains_reference_for_later_scoped_resolution(transport):
    transport.return_value = response([row(19, order_id=None, order_reference="R123456789")])
    page = await read("refund_requests")
    assert page["changes"][0]["record_keys"] == [("customrecord_fw_refund_requests", "19")]
    assert page["changes"][0]["order_id"] is None
    assert page["changes"][0]["order_reference"] == "R123456789"


@pytest.mark.parametrize(
    "rows",
    [
        [row(2), row(1)],
        [row(1), row(1)],
        [row(True)],
        [row(0)],
        [row(1, subsidiary="3")],
        [row(1, type="Journal")],
        [row(1, modified_utc="2026-09-07T00:00:00Z")],
        [row(1, modified_utc="2026-09-06T18:21:37")],
    ],
)
async def test_bad_scope_cursor_or_time_never_establishes_coverage(transport, rows):
    transport.return_value = response(rows)
    with pytest.raises(reader.NetSuiteEvidenceError, match="dependency_change_page_incomplete"):
        await read()


@pytest.mark.parametrize("change", [{"hasMore": True}, {"count": 0}, {"totalResults": 0}, {"offset": 1}])
async def test_inconsistent_or_short_page_cannot_claim_complete(transport, change):
    transport.return_value = {**response([row()]), **change}
    with pytest.raises(reader.NetSuiteEvidenceError, match="dependency_change_page_incomplete"):
        await read()


@pytest.mark.parametrize(
    "kwargs", [{"after": [True]}, {"after": ["1 OR 1=1"]}, {"after": [1, 2]}, {"page_size": 251}, {"page_size": True}]
)
async def test_invalid_page_never_reaches_provider(transport, kwargs):
    with pytest.raises(reader.NetSuiteEvidenceError, match="invalid_dependency_change_scope"):
        await read(**kwargs)
    transport.assert_not_awaited()


async def test_wrong_tenant_is_rejected_before_provider_read(db, admin_user, tenant_b):
    from tests.test_transaction_defaults import connections

    _, target = await connections(db, admin_user[0].tenant_id)
    with pytest.raises(reader.NetSuiteEvidenceError, match="invalid_connection"):
        await reader.read_change_page(
            db,
            tenant_b.id,
            target.id,
            "6738075",
            "2",
            "tranid",
            "transactions",
            START,
            END,
        )


async def test_bulk_page_retains_probe_row_and_exact_cursor(transport):
    transport.return_value = response([row(i) for i in range(1, 252)], True)
    value = await read(page_size=250)
    assert len(value["changes"]) == 250
    assert value["next_cursor"] == [250] and not value["scan_complete"]
    assert transport.call_args.kwargs["params"]["limit"] == 251
