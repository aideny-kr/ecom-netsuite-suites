from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.chat.write_payload import PayloadParseError, normalize_write_payload
from app.services.transaction_ops import tax_correction as mod


def fixture():
    source = dict(
        id="1",
        number="R123",
        state="complete",
        completed_at="2026-08-01",
        business_entity="Company",
        currency="USD",
        total="7046.00",
        tax_total="335.52",
        included_tax_total="335.52",
        additional_tax_total="0",
        ship_total="0",
        adjustment_total="0",
        item_total="7046.00",
        line_items=[
            dict(
                id="line1",
                total="7046.00",
                adjustments=[
                    dict(amount="335.52", source_type="Spree::TaxRate", finalized=True, label="VAT (Included in Price)")
                ],
            )
        ],
    )
    report = dict(
        order_reference="R123",
        source=dict(record_id="1", currency="USD", currency_minor_unit=2, total="7046.00", tax="335.52"),
    )
    review = dict(
        native_mcp_connector_id=str(uuid4()),
        netsuite_connection_id=str(uuid4()),
        scope=dict(netsuite_account_id="123", subsidiary_id="1"),
        business_entity_subsidiaries={"Company": "1"},
    )
    doc = dict(
        id="20",
        record_type="invoice",
        lastModifiedDate="2026-08-01T00:00:00Z",
        total="7030.02",
        taxTotal="319.54",
        subtotal="6710.48",
        taxRate="4.7618507",
        exchangeRate="1",
        postingPeriod={"id": "40"},
        currency={"id": "1"},
        subsidiary={"id": "1"},
        taxItem={"id": "30"},
        createdFrom={"id": "10"},
        amountPaid="7030.02",
        amountRemaining="0",
    )
    evidence = dict(
        observed_at=datetime.now(timezone.utc).isoformat(),
        sections={
            "posting_documents": [doc],
            "linked_documents": {"complete": True},
            "postingPeriod": [dict(id="40", closed=False, arLocked=True, allLocked=True)],
            "taxItem": [dict(id="30", isInactive=False)],
            "gl": {
                "20": {
                    "complete": True,
                    "rows": [
                        dict(account="119", accountingbook="1", debit="7030.02", credit=None),
                        dict(account="210", accountingbook="1", credit="319.54", debit=None),
                        dict(account="54", accountingbook="1", credit="6710.48", debit=None),
                    ],
                }
            },
        },
    )
    return evidence, report, review, source


def proposal():
    e, r, v, s = fixture()
    p = mod.candidate(e, r, v, s)
    assert p
    p.update(tenant_id="tenant", case_id="case")
    return p


def test_effective_rate_uses_source_vat_over_net_without_guessing_statutory_rate():
    p = proposal()
    assert p["proposed_fields"] == {"taxRate": 4.9999404}
    assert p["expected_after"] == {"total": "7046.00", "taxTotal": "335.52"}
    assert p["before"]["taxRate"] == "4.7618507"
    assert p["period"]["closed"] is False and p["period"]["allLocked"] is True
    assert "Override Period Restrictions" in p["approval_basis"]
    assert "does not settle cash" in p["approval_basis"]


@pytest.mark.parametrize(
    "problem",
    [
        "source_changed",
        "entity",
        "currency",
        "credit",
        "partial",
        "multi_invoice",
        "closed",
        "no_gl",
        "fx",
        "net_mismatch",
        "unfinalized_tax",
        "wrong_line_tax",
        "different_error",
    ],
)
def test_candidate_is_absent_when_scope_or_accounting_basis_is_not_proven(problem):
    e, r, v, s = fixture()
    if problem == "source_changed":
        s["total"] = "7046.01"
    elif problem == "entity":
        s["business_entity"] = "Other"
    elif problem == "currency":
        s["currency"] = "CAD"
    elif problem == "credit":
        s["adjustment_total"] = "-1"
    elif problem == "partial":
        e["sections"]["linked_documents"]["complete"] = False
    elif problem == "multi_invoice":
        e["sections"]["posting_documents"] *= 2
    elif problem == "closed":
        e["sections"]["postingPeriod"][0]["closed"] = True
    elif problem == "no_gl":
        e["sections"]["gl"]["20"]["complete"] = False
    elif problem == "fx":
        e["sections"]["posting_documents"][0]["exchangeRate"] = "1.2"
    elif problem == "net_mismatch":
        e["sections"]["posting_documents"][0]["subtotal"] = "6711.48"
    elif problem == "unfinalized_tax":
        s["line_items"][0]["adjustments"][0]["finalized"] = False
    elif problem == "wrong_line_tax":
        s["line_items"][0]["adjustments"][0]["amount"] = "335.51"
    elif problem == "different_error":
        e["sections"]["posting_documents"][0]["taxRate"] = "4.76"
    assert mod.candidate(e, r, v, s) is None


def test_exact_scoped_candidate_is_required_for_tax_card_and_record_id_is_preserved():
    p = proposal()
    db = SimpleNamespace(info={"accounting_correction_candidate": p})
    name = f"ext__{p['connector_id'].replace('-', '')}__ns_updateRecord"
    params = {"recordId": "20", "data": {"taxRate": 4.9999404}}
    normalized = normalize_write_payload(params)
    assert normalized.record_id == "20"
    assert mod.review_for_card(db, "tenant", name, "invoice", normalized) == p
    for tenant, fields in [("other", {"taxRate": 4.9999404}), ("tenant", {"taxRate": 6.0})]:
        with pytest.raises(ValueError):
            mod.review_for_card(db, tenant, name, "invoice", normalize_write_payload({**params, "data": fields}))
    with pytest.raises(PayloadParseError):
        normalize_write_payload({**params, "id": "99"})


@pytest.fixture
def native(monkeypatch):
    p = proposal()
    doc = deepcopy(p["before"])
    period = deepcopy(p["period"])
    reader = SimpleNamespace(
        request=AsyncMock(
            side_effect=lambda method, path, **kw: (
                period if "accountingPeriod" in path else p["tax_item"] if "salesTaxItem" in path else doc
            )
        )
    )

    @asynccontextmanager
    async def auth(*args, **kwargs):
        yield reader

    monkeypatch.setattr("app.services.transaction_ops.netsuite_reader.authenticated_reader", auth)
    source = AsyncMock(return_value=deepcopy(p["source"]))
    monkeypatch.setattr(mod, "refresh_source", source)
    return p, doc, period, reader, source


async def test_preapproval_snapshot_can_revalidate_without_model_or_write(native):
    p, _, _, reader, _ = native
    await mod.revalidate(None, "tenant", p)
    assert reader.request.await_count == 3
    assert all(c.args[0] == "GET" for c in reader.request.await_args_list)


@pytest.mark.parametrize("change", ["source", "amount", "version", "currency", "period"])
async def test_stale_evidence_never_reaches_the_approved_update(native, change):
    p, doc, period, reader, source = native
    if change == "source":
        source.return_value["total"] = "7047"
    elif change == "amount":
        doc["total"] = "7047"
    elif change == "version":
        doc["lastModifiedDate"] = "2026-09-10T00:00:00Z"
    elif change == "currency":
        doc["currency"] = {"id": "2"}
    elif change == "period":
        period["closed"] = True
    with pytest.raises(ValueError):
        await mod.revalidate(None, "tenant", p)
    assert all(c.args[0] == "GET" for c in reader.request.await_args_list)


@pytest.mark.parametrize("gl_correct", [True, False])
async def test_success_requires_native_invoice_and_gl_not_just_receipt(native, gl_correct):
    p, doc, _, reader, _ = native
    doc.update(total="7046.00", taxTotal="335.52", taxRate="4.9999404")
    rows = deepcopy(p["before_gl"])
    if gl_correct:
        rows[0]["debit"] = "7046.00"
        rows[1]["credit"] = "335.52"
    reader.request.side_effect = lambda method, path, **kw: (
        doc if method == "GET" else dict(items=rows, count=3, totalResults=3, hasMore=False)
    )
    result = await mod.verify_after(None, "tenant", p)
    assert result["status"] == ("verified" if gl_correct else "needs_review")
    assert result["cash_settlement"] == "not_verified"
    assert result["case_settlement"] == "not_verified"
