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


async def test_source_ineligible_group_triage_reads_identity_and_amounts_but_marks_detail_deferred(native):
    reader = native[0]
    result = await mod.collect_accounting_evidence(
        None, "tenant", review(), {"order_reference": "R123", "source": {"currency": "USD"}}, posting_detail=False
    )
    assert reader.calls == 3  # SO, links, invoice; currency is a fixture-local lookup.
    assert result["sections"]["sales_order"]["id"] == "10"
    assert result["sections"]["posting_documents"][0]["id"] == "20"
    assert result["deferred_sections"] == ["gl", "deposits", "taxItem", "postingPeriod"]
    assert all(k not in result["sections"] for k in result["deferred_sections"])
    assert result["assessment"]["correction_ready"] is False
    assert "posting_detail_deferred_until_supported_treatment_is_identified" in result["blockers"]


async def test_expanded_native_lines_are_available_for_identity_and_price_investigation(native):
    native[1]["invoice/20"]["item"] = {
        "items": [
            {
                "line": 1,
                "item": {"id": "91", "refName": "Memory"},
                "quantity": 1,
                "rate": "1600.00",
                "amount": "1600.00",
                "custcol_fw_solidus_line_id": "60774032",
                "custcol_fw_item_sku": "MEMORY-64",
                "private_field": "omit",
            }
        ],
        "links": [],
    }
    result = await collect()
    line_evidence = result["sections"]["posting_documents"][0]["line_evidence"]
    assert line_evidence["complete"] is True
    assert line_evidence["lines"][0]["rate"] == "1600.00"
    assert line_evidence["lines"][0]["custcol_fw_solidus_line_id"] == "60774032"
    assert "private_field" not in line_evidence["lines"][0]
    assert native[0].calls == 7  # No additional upstream read for already expanded lines.


@pytest.mark.parametrize("items", [None, {"items": [], "hasMore": True}, {"items": [], "totalResults": 1}])
async def test_missing_or_partial_lines_are_not_certified(native, items):
    native[1]["invoice/20"]["item"] = items
    result = await collect()
    evidence = result["sections"]["posting_documents"][0]["line_evidence"]
    assert evidence["complete"] is False
    assert evidence["problems"]


def refund_report():
    return {
        "order_reference": "R123",
        "source": {"currency": "USD"},
        "refund_evidence": {
            "source": {
                "order_reference": "R123",
                "currency": "USD",
                "amount": "440.00",
                "complete": True,
                "observed_at": "2026-09-13T00:00:00Z",
            },
            "target": {
                "order_reference": "R123",
                "currency": "USD",
                "account_id": "123-sb1",
                "subsidiary_id": "1",
                "connection_id": "connection",
                "amount": "440.00",
                "complete": True,
                "request_links": [
                    {
                        "credit_memo_id": "71",
                        "refund_id": "72",
                        "request_id": "73",
                        "amount": "440.00",
                        "stage": "refund_verified",
                    }
                ],
            },
        },
    }


def test_existing_credit_and_refund_identifiers_survive_as_historical_evidence():
    evidence = mod.historical_refund_context(review(), refund_report())
    assert evidence["request_links"][0]["credit_memo_id"] == "71"
    assert evidence["request_links"][0]["refund_id"] == "72"
    assert "Historical" in evidence["authority"]
    assert "Re-read" in evidence["authority"]
    assert evidence["source"]["observed_at"] == "2026-09-13T00:00:00Z"
    assert "correction_candidate" not in evidence


@pytest.mark.parametrize("field", ["account_id", "subsidiary_id", "connection_id", "order_reference", "currency"])
def test_other_scope_refund_identifiers_are_not_reused(field):
    report = refund_report()
    report["refund_evidence"]["target"][field] = "other"
    assert mod.historical_refund_context(review(), report) is None


@pytest.mark.parametrize("value", [[], "bad", {"source": []}, {"source": "bad"}])
def test_malformed_historical_refund_evidence_does_not_break_investigation(value):
    report = refund_report()
    report["refund_evidence"] = value
    assert mod.historical_refund_context(review(), report) is None


async def test_saved_section_error_explains_both_recovery_paths_without_any_read(monkeypatch):
    from unittest.mock import AsyncMock

    from app.mcp.tools import transaction_ops_tools

    authorize = AsyncMock()
    monkeypatch.setattr(transaction_ops_tools, "_authorize", authorize)
    result = await transaction_ops_tools.execute_accounting_evidence({"case_id": "case", "section": "applications"})
    assert result["success"] is False
    assert result["reason"] == "missing_observation_id"
    assert "audit_id" in result["recovery"]["saved_read"]
    assert "only case_id" in result["recovery"]["fresh_read"]
    authorize.assert_not_awaited()


@pytest.fixture
def existing_refund(native):
    documents = native[1]
    documents["creditMemo/71"] = {
        "id": "71",
        "subsidiary": {"id": "1"},
        "currency": {"id": "1"},
        "custbody_fw_order_number": "R123",
        "total": "440.00",
        "applied": "440.00",
        "unapplied": "0",
        "isTaxable": False,
        "tranId": "CM71",
        "item": {"items": [{"line": 1, "amount": "440.00", "item": {"id": "1603"}}]},
        "apply": {"items": [{"apply": True, "doc": {"id": "72"}, "amount": "440.00"}]},
    }
    documents["customerRefund/72"] = {
        "id": "72",
        "subsidiary": {"id": "1"},
        "currency": {"id": "1"},
        "total": "440.00",
        "apply": {"items": [{"apply": True, "doc": {"id": "71"}, "amount": "440.00"}]},
    }
    return documents


async def test_refresh_existing_credit_refund_and_gl_without_inventing_zero_tax(native, existing_refund):
    from app.services.transaction_ops.record_links import evidence_record_links

    result = await mod.collect_accounting_evidence(None, "tenant", review(), refund_report())
    related = result["sections"]["related_refund_documents"]
    assert related["complete"] is False  # Selected records do not establish graph completeness.
    credit, refund = related["documents"]
    assert credit["applied"] == "440.00"
    assert credit["isTaxable"] is False
    assert "taxTotal" not in credit  # Omitted tax is not synthesized as zero.
    assert credit["application_evidence"]["complete"] is True
    assert refund["application_evidence"]["lines"][0]["doc"]["id"] == "71"
    assert result["sections"]["gl"]["71"]["complete"] is True
    assert native[0].calls == 10  # Only the known credit, its GL and linked refund were added.
    links = evidence_record_links(result)
    assert any(x["record_id"] == "72" and "custrfnd.nl?id=72" in x["url"] for x in links)
    assert result["assessment"]["executable_proposal"] is None


@pytest.mark.parametrize("field,value", [("custbody_fw_order_number", "OTHER"), ("subsidiary", {"id": "2"})])
async def test_historical_credit_scope_conflict_does_not_authorize_refund_read(native, existing_refund, field, value):
    existing_refund["creditMemo/71"][field] = value
    result = await mod.collect_accounting_evidence(None, "tenant", review(), refund_report())
    assert result["sections"]["related_refund_documents"]["documents"] == []
    assert native[0].calls == 8
    assert any("credit:" in b for b in result["blockers"])


async def test_missing_credit_application_does_not_imply_refund_exists(native, existing_refund):
    existing_refund["creditMemo/71"].pop("apply")
    result = await mod.collect_accounting_evidence(None, "tenant", review(), refund_report())
    docs = result["sections"]["related_refund_documents"]["documents"]
    assert len(docs) == 1
    assert docs[0]["application_evidence"]["complete"] is False
    assert native[0].calls == 9


async def test_group_first_pass_retains_refund_leads_without_gl_or_refund_reads(native, existing_refund):
    result = await mod.collect_accounting_evidence(None, "tenant", review(), refund_report(), posting_detail=False)
    assert "historical_refunds" in result["sections"]
    assert "related_refund_documents" not in result["sections"]
    assert native[0].calls == 3


def test_source_retains_tax_geography_without_full_address_or_contact():
    from app.services.transaction_ops.source_projection import project_order

    source = {
        "number": "R123",
        "total": "100.00",
        "ship_address": {
            "name": "Private Name",
            "phone": "private",
            "address1": "Private street",
            "email": "private",
            "zipcode": "94103",
            "country": {"id": 1, "iso": "US", "secret": "private"},
            "state": {"id": 5, "abbr": "CA"},
        },
    }
    projected = project_order(source)
    assert projected["tax_jurisdiction"] == {
        "zipcode": "94103",
        "country": {"id": "1", "iso": "US"},
        "state": {"id": "5", "abbr": "CA"},
    }
    assert "private" not in str(projected).lower()
    assert "ship_address" not in projected


def test_bad_optional_tax_geography_does_not_break_existing_monetary_read():
    from app.services.transaction_ops.source_projection import project_order

    projected = project_order({"number": "R123", "total": "100.00", "ship_address": {"country": "bad shape"}})
    assert projected["total"] == "100.00"
    assert projected["tax_jurisdiction"]["problem"] == "invalid_source_tax_jurisdiction"


async def test_native_tax_geography_excludes_full_address(native):
    native[1]["invoice/20"]["shippingAddress"] = {
        "country": {"id": "US", "refName": "United States", "unexpected": "omit"},
        "state": "CA",
        "zip": "94103",
        "addr1": "private",
        "addressee": "private",
        "addrPhone": "private",
    }
    result = await collect()
    doc = result["sections"]["posting_documents"][0]
    assert doc["tax_jurisdiction"] == {
        "country": {"id": "US", "refName": "United States"},
        "state": "CA",
        "zip": "94103",
    }
    assert "private" not in str(doc)


async def test_completion_summary_preserves_existing_credit_without_claiming_full_graph(native, existing_refund):
    evidence = await mod.collect_accounting_evidence(None, "tenant", review(), refund_report())
    evidence["audit_id"] = "audit"
    result = mod.completion_evidence_summary(evidence)
    credit = next(r for r in result["documents"] if r["record_type"] == "creditmemo")
    assert credit["applied"] == "440.00"
    assert "taxTotal" not in credit
    assert result["related_refund_graph_complete"] is False
    assert result["audit_id"] == "audit"
    assert result["assessment"]["executable_proposal"] is None


async def test_richer_source_observations_do_not_change_existing_approval_snapshot(monkeypatch):
    from unittest.mock import AsyncMock

    from app.services.transaction_ops import tax_correction

    old_snapshot = {"id": "1", "number": "R123", "total": "100.00", "currency": "USD"}
    detail = {"taxes": [], "shipments": [], "tax_jurisdiction": {"country_iso": "US"}}
    read = AsyncMock(return_value={"orders": [{**old_snapshot, **detail}]})
    monkeypatch.setattr(tax_correction, "read_framework_order", read)
    legacy = await tax_correction.refresh_source(None, "tenant", {}, "R123")
    rich = await tax_correction.refresh_source(None, "tenant", {}, "R123", include_accounting_detail=True)
    assert legacy == old_snapshot
    assert rich == {**old_snapshot, **detail}
    assert {k: v for k, v in rich.items() if k not in tax_correction.ACCOUNTING_DETAIL_SOURCE_FIELDS} == old_snapshot
    assert read.await_count == 2
