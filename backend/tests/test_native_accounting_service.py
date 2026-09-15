import json
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.services.transaction_ops import native_accounting_protocol as protocol
from app.services.transaction_ops import native_accounting_service as service
from app.services.transaction_ops.accounting_field_map import LEGACY_FIELDS
from app.services.transaction_ops.accounting_preview import for_intent
from app.services.transaction_ops.credit_reallocation import build_intent
from app.services.transaction_ops.resolution_plan import fingerprint, operation_identity
from tests.test_credit_reallocation import fixture
from tests.test_native_accounting_profile import profile


def prepared():
    source, review, evidence, support = fixture()
    review["scope"]["netsuite_account_id"] = "123456-sb1"
    support["credit"]["lastModifiedDate"] = "2026-09-01T00:00:00Z"
    support["credit"]["application_evidence"] = {
        "complete": True,
        "lines": [{"apply": True, "doc": "31", "amount": "440"}],
    }
    p = build_intent("tenant", "case", source, review, evidence, support)
    p["before"] = deepcopy(p["before"])
    p["before"]["taxTotal"] = "0"
    configured = profile("123456-sb1", "1")
    configured["fields"] = dict(LEGACY_FIELDS)
    configured["revision"] = fingerprint(configured)
    request = for_intent(p, review, p["before"], field_map=configured["fields"])
    body = {
        "entity": "7",
        "account": "11",
        "subsidiary": "1",
        "currency": "1",
        "total": "440",
        "subtotal": "440",
        "taxtotal": "0",
        "applied": "440",
        "unapplied": "0",
        "istaxable": False,
        "taxitem": None,
        "taxrate": "0",
        "lastmodifieddate": "2026-09-01T00:00:00Z",
        protocol.WORK_FIELD: None,
        configured["fields"]["order_reference"]: "R123",
    }
    lines = [
        {
            "line": "1",
            "lineuniquekey": "100",
            "item": "4",
            "itemtype": "NonInvtPart",
            "quantity": "1",
            "quantityfulfilled": "0",
            "quantitybilled": "0",
            "rate": "440",
            "amount": "440",
            "istaxable": False,
        }
    ]
    before = {"body": body, "lines": lines}
    amendment = json.loads(request["amendmentJson"])
    expected = json.loads(request["expectedJson"])
    after = deepcopy(before)
    after["body"].update(amendment["body"])
    after["body"].update(expected)
    after["lines"][0].update(amendment["lines"][0]["fields"])
    preview = {
        **{key: request[key] for key in ("accountId", "recordType", "recordId", "subsidiaryId", "currencyId")},
        "profile": protocol.profile_binding(configured),
        "schema_version": 1,
        "roleId": "7",
        "taxRegime": "legacy",
        "success": True,
        "saved": False,
        "financialWrites": 0,
        "executionAuthorized": False,
        "matches": True,
        "amendment": amendment,
        "after": expected,
        "beforeSnapshot": before,
        "afterSnapshot": after,
    }
    p.update(
        native_profile=configured,
        native_request=request,
        native_preview=preview,
        observed_at=datetime.now(timezone.utc).isoformat(),
        status="ready_for_exact_human_approval",
    )
    return p, (source, review, evidence, support, configured)


def posted(proposal):
    after = deepcopy(proposal["native_preview"]["afterSnapshot"])
    after["body"][protocol.WORK_FIELD] = operation_identity(proposal)
    after["body"]["lastmodifieddate"] = "2026-09-01T00:01:00Z"
    return after


def test_signed_binding_and_snapshot_independently_check_native_results():
    p, _ = prepared()
    service.validate_binding("tenant", service.TOOL, service.signed_input(p), p)
    assert protocol.verify_snapshot(
        p["native_preview"]["beforeSnapshot"],
        posted(p),
        json.loads(p["native_request"]["amendmentJson"]),
        json.loads(p["native_request"]["expectedJson"]),
        work_key=operation_identity(p),
    )


@pytest.mark.parametrize(
    "change",
    [
        "quantity",
        "quantityfulfilled",
        "quantitybilled",
        "line",
        "lineuniquekey",
        "amount",
        "total",
        "taxitem",
        "applied",
        "unapplied",
        "source_ref",
        "missing",
        "extra",
        "work_key",
    ],
)
def test_native_protected_and_declared_values_must_all_agree(change):
    p, _ = prepared()
    after = posted(p)
    if change in {"quantity", "quantityfulfilled", "quantitybilled", "line", "lineuniquekey", "amount"}:
        after["lines"][0][change] = "999"
    elif change in {"total", "taxitem", "applied", "unapplied"}:
        after["body"][change] = "999"
    elif change == "source_ref":
        after["body"][LEGACY_FIELDS["order_reference"]] = "R999"
    elif change == "missing":
        del after["body"]["applied"]
    elif change == "extra":
        after["body"]["unexpected"] = "1"
    else:
        after["body"][protocol.WORK_FIELD] = "b" * 64
    with pytest.raises(ValueError):
        protocol.verify_snapshot(
            p["native_preview"]["beforeSnapshot"],
            after,
            json.loads(p["native_request"]["amendmentJson"]),
            json.loads(p["native_request"]["expectedJson"]),
            work_key=operation_identity(p),
        )


@pytest.mark.parametrize("change", ["profile", "source", "ledger", "native_request", "preview", "kind", "record"])
def test_approval_input_binds_all_proposal_evidence(change):
    p, _ = prepared()
    params = service.signed_input(p)
    if change == "profile":
        p["native_profile"]["role_id"] = "9"
    elif change == "source":
        p["source"]["updated_at"] = "changed"
    elif change == "ledger":
        p["expected_ledger"]["debit"]["13"] = "41"
    elif change == "native_request":
        p["native_request"]["recordId"] = "999"
    elif change == "preview":
        p["native_preview"]["beforeSnapshot"]["body"]["applied"] = "0"
    elif change == "kind":
        p["kind"] = "invoice_tax"
    else:
        p["record_id"] = "999"
    with pytest.raises(ValueError):
        service.validate_binding("tenant", service.TOOL, params, p)


@pytest.mark.parametrize("change", ["none", "ledger", "invoice", "refund", "application", "penny", "identity"])
async def test_posting_verification_requires_gl_related_records_and_exact_native_values(monkeypatch, change):
    p, fresh = prepared()
    source, review, evidence, support, configured = deepcopy(fresh)
    support["credit_gl"]["rows"] = [
        {"account": "12", "accountingbook": "1", "debit": "400", "credit": None},
        {"account": "13", "accountingbook": "1", "debit": "40", "credit": None},
        {"account": "11", "accountingbook": "1", "debit": None, "credit": "440"},
    ]
    after = posted(p)
    support["credit"].update(
        taxTotal="40", subtotal="400", taxRate="10.0", isTaxable=True, lastModifiedDate="2026-09-01T00:01:00Z"
    )
    support["credit"]["line_evidence"]["lines"][0].update(rate="400", amount="400", isTaxable=True)
    if change == "ledger":
        support["credit_gl"]["rows"][0]["debit"] = "440"
    if change == "invoice":
        support["invoice"]["amountPaid"] = "1320"
    if change == "refund":
        support["refund"]["total"] = "0"
    if change == "application":
        support["credit"]["application_evidence"]["lines"][0]["amount"] = "400"
    if change == "penny":
        after["body"]["taxtotal"] = "40.01"
    monkeypatch.setattr(service, "_fresh", AsyncMock(return_value=(source, review, evidence, support, configured)))
    reader = AsyncMock(
        return_value={
            "success": True,
            "schema_version": 1,
            "profile": protocol.profile_binding(configured),
            "record_type": "creditmemo",
            "record_id": "30",
            "native_snapshot": after,
            "financial_writes": 0,
            "execution_authorized": False,
        }
    )
    monkeypatch.setattr(service, "_read", reader)
    result = await service.verify_after(None, "tenant", p, {"record_id": "999"} if change == "identity" else None)
    assert result["status"] == ("verified" if change == "none" else "needs_review")
    assert result["retry_allowed"] is False
    if change == "none":
        assert result["full_reconciliation_required"] is True
    if change == "identity":
        reader.assert_not_awaited()


@pytest.mark.parametrize("change", ["source", "profile", "role", "tax", "enabled", "amount"])
async def test_candidate_never_issues_approval_from_missing_or_changed_native_authority(monkeypatch, change):
    p, (_, review, _, _, configured) = prepared()
    capabilities = {
        "success": True,
        "schema_version": 1,
        "profile": protocol.profile_binding(configured),
        "financial_writes": 0,
        "execution_authorized": False,
        "suitetax": False,
        "treatments": list(protocol.KINDS),
        "apply_enabled": True,
    }
    response = deepcopy(p["native_preview"])
    if change == "source":
        response["beforeSnapshot"]["body"][LEGACY_FIELDS["order_reference"]] = "OTHER"
    if change == "profile":
        capabilities["profile"]["account_id"] = "another"
    if change == "role":
        response["roleId"] = "9"
    if change == "tax":
        capabilities["suitetax"] = True
    if change == "enabled":
        capabilities["apply_enabled"] = False
    if change == "amount":
        response["after"]["total"] = "440.01"
    monkeypatch.setattr(service, "_read", AsyncMock(side_effect=[capabilities, response]))
    with pytest.raises(ValueError):
        await service.preview_candidate(None, "tenant", p, review, configured)
