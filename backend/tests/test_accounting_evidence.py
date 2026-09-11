from contextlib import asynccontextmanager

import pytest

from app.services.transaction_ops import accounting_evidence as mod
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError


def collection(rows, complete=True):
    return dict(items=rows, count=len(rows), totalResults=len(rows), hasMore=not complete)


@pytest.fixture
def native(monkeypatch):
    documents = {
        "salesOrder/10": dict(
            id="10",
            tranId="R123",
            status={"id": "G", "refName": "Billed"},
            subsidiary={"id": "1"},
            currency={"id": "1"},
        ),
        "invoice/20": dict(
            id="20",
            createdFrom={"id": "10"},
            subsidiary={"id": "1"},
            currency={"id": "1"},
            total="984.02",
            taxTotal="84.02",
            taxRate="4.76",
            taxItem={"id": "30"},
            postingPeriod={"id": "40"},
            status={"refName": "Paid In Full"},
            email="private@example.com",
        ),
        "salesTaxItem/30": dict(id="30", rate="0", itemId="EXTERNAL"),
        "accountingPeriod/40": dict(id="40", closed=False, arLocked=True, allLocked=True),
        "customerDeposit/50": dict(
            id="50", salesOrder={"id": "10"}, subsidiary={"id": "1"}, currency={"id": "1"}, payment="1000.00"
        ),
    }

    class Reader:
        calls = 0
        queries = []
        currency_code = "USD"
        links_complete = True
        failure = None

        async def currency(self, identifier):
            return dict(id=identifier, symbol=self.currency_code)

        async def request(self, method, path, **kwargs):
            self.calls += 1
            if path == self.failure:
                raise NetSuiteEvidenceError("upstream_http_403")
            if method == "POST":
                assert path == "/query/v1/suiteql"
                sql = kwargs["body"]["q"]
                assert "t.subsidiary = 1" in sql
                self.queries.append(sql)
                if "DISTINCT" in sql:
                    return collection(
                        [dict(id="20", type="CustInvc"), dict(id="50", type="CustDep")], self.links_complete
                    )
                return collection([dict(account="210", credit="84.02")])
            assert method == "GET"
            return documents[path.removeprefix("/record/v1/")]

    reader = Reader()
    calls = []

    @asynccontextmanager
    async def auth(db, tenant, connection, account, **kwargs):
        calls.append((tenant, connection, account, kwargs))
        yield reader

    monkeypatch.setattr(mod, "authenticated_reader", auth)
    return reader, documents, calls


def review():
    return dict(
        scope=dict(record_type="salesorder", subsidiary_id="1", netsuite_account_id="123-sb1"),
        configuration_status="scoped_configuration_found",
        connection_active=True,
        netsuite_connection_id="connection",
        observed_scope=dict(status="consistent_in_stored_observation", target_records=[dict(record_id="10")]),
    )


async def collect(scope=None):
    return await mod.collect_accounting_evidence(
        None, "tenant", scope or review(), dict(order_reference="R123", source=dict(currency="USD"))
    )


async def test_native_labels_locks_and_rates_preserved_without_root_cause_or_cash_inference(native):
    reader, _, calls = native
    result = await collect()
    assert result["sections"]["sales_order"]["status"]["refName"] == "Billed"
    assert result["sections"]["postingPeriod"][0]["closed"] is False
    assert result["sections"]["postingPeriod"][0]["allLocked"] is True
    assert result["sections"]["taxItem"][0]["rate"] == "0"
    assert result["sections"]["posting_documents"][0]["taxRate"] == "4.76"
    assert "email" not in result["sections"]["posting_documents"][0]
    assert result["assessment"]["root_cause"] == "not_verified"
    assert result["assessment"]["unapplied_deposit_balance"] == "not_verified"
    assert result["assessment"]["correction_ready"] is False
    assert result["verified_connection_scope"]["account_id"] == "123-sb1"
    assert calls[0][:3] == ("tenant", "connection", "123-sb1")
    assert reader.calls == 7


@pytest.mark.parametrize("conflict", ["account", "ambiguous", "missing_id"])
async def test_invalid_case_scope_never_contacts_netsuite(native, conflict):
    scope = review()
    if conflict == "account":
        scope["observed_scope"]["status"] = "conflict"
    elif conflict == "ambiguous":
        scope["configuration_status"] = "ambiguous"
    else:
        scope["observed_scope"]["target_records"][0]["record_id"] = "10 OR 1=1"
    result = await collect(scope)
    assert result["blockers"] == [
        "ambiguous_reconciliation_configuration"
        if conflict == "ambiguous"
        else "verified_unique_sales_order_scope_required"
    ]
    assert not native[2]


@pytest.mark.parametrize("field,value", [("subsidiary", {"id": "2"}), ("id", "99"), ("tranId", "R999")])
async def test_current_order_identity_conflict_stops_dependent_reads(native, field, value):
    native[1]["salesOrder/10"][field] = value
    result = await collect()
    assert result["blockers"]
    assert not native[0].queries


async def test_currency_conflict_stops_dependent_reads(native):
    native[0].currency_code = "EUR"
    result = await collect()
    assert "sales_order:currency_conflict" in result["blockers"]
    assert not native[0].queries


async def test_failed_period_read_preserves_observed_invoice_and_specific_blocker(native):
    native[0].failure = "/record/v1/accountingPeriod/40"
    result = await collect()
    assert result["sections"]["posting_documents"][0]["id"] == "20"
    assert "postingPeriod:upstream_http_403" in result["blockers"]
    assert "gl" in result["sections"]


async def test_partial_links_and_conflicting_origin_cannot_be_complete(native):
    native[0].links_complete = False
    native[1]["invoice/20"]["createdFrom"] = {"id": "999"}
    result = await collect()
    assert "linked_documents:incomplete_collection" in result["blockers"]
    assert "invoice:20:origin_conflict" in result["blockers"]
    assert not result["sections"]["posting_documents"]
    assert "gl" not in result["sections"]
