from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.services.transaction_ops import case_resolution_scope as scope
from app.services.transaction_ops.resolution_plan import proposal_plan
from tests.test_native_accounting_service import prepared


def restriction():
    return {
        "audit_id": "scope-audit",
        "allowed_mutations": [{"kind": "credit_tax_reallocation", "record_type": "creditmemo", "record_id": "40"}],
        "preserve_records": [
            {"record_type": "invoice", "record_id": "31"},
            {"record_type": "salesorder", "record_id": "30"},
        ],
    }


def test_only_exact_credit_is_allowed_without_changing_unrestricted_cases():
    value = restriction()
    credit = value["allowed_mutations"][0]
    assert scope.allows(value, credit)
    assert not scope.allows(value, {**credit, "record_id": "41"})
    assert not scope.allows(
        value, {"kind": "sales_order_line_alignment", "record_type": "salesorder", "record_id": "30"}
    )
    assert not scope.allows(value, {**credit, "kind": "sales_adjustment_credit"})
    assert scope.allows(None, {"kind": "invoice_tax"})


async def test_new_scope_invalidates_old_approval_and_blocks_other_targets(monkeypatch):
    value = restriction()
    monkeypatch.setattr(scope, "load", AsyncMock(return_value=value))
    proposal = {**value["allowed_mutations"][0], "case_id": "case"}
    with pytest.raises(ValueError, match="scope_changed"):
        await scope.validate(AsyncMock(), "tenant", proposal)
    proposal["resolution_scope"] = deepcopy(value)
    await scope.validate(AsyncMock(), "tenant", proposal)
    proposal["record_id"] = "41"
    with pytest.raises(ValueError, match="outside_case_resolution_scope"):
        await scope.validate(AsyncMock(), "tenant", proposal)


def test_credit_only_plan_preserves_documents_without_dependent_amendment():
    proposal, _ = prepared()
    proposal["resolution_scope"] = restriction()
    plan = proposal_plan(proposal, {})
    assert not any(step["id"] == "sales_order" for step in plan["steps"])
    preservation = next(step for step in plan["steps"] if step["id"] == "preserved_records")
    assert preservation["read_only"] is True
    assert preservation["records"] == restriction()["preserve_records"]
    unrestricted = deepcopy(proposal)
    unrestricted.pop("resolution_scope")
    assert any(step["id"] == "sales_order" for step in proposal_plan(unrestricted, {})["steps"])


def test_api_credit_plan_does_not_infer_an_original_order_error():
    proposal, _ = prepared()
    proposal["execution_transport"] = "mcp_record_api"
    plan = proposal_plan(proposal, {})
    assert not any(step["id"] == "sales_order" for step in plan["steps"])
    assert next(step for step in plan["steps"] if step["id"] == "preserved_records")["read_only"]


async def test_credit_completion_requires_independent_evidence_for_other_amendments(monkeypatch):
    from types import SimpleNamespace

    from app.services.transaction_ops.accounting_completion import prepare_next

    proposal, _ = prepared()
    proposal["execution_transport"] = "mcp_record_api"
    monkeypatch.setattr(scope, "load", AsyncMock(return_value=None))
    card, result = await prepare_next(
        AsyncMock(), proposal["tenant_id"], SimpleNamespace(structured_output={"accounting_review": proposal}), "actor"
    )
    assert card is None
    assert result["status"] == "independent_review_required"


async def test_persisted_scope_is_case_and_tenant_bound(db, admin_user, admin_user_b):
    from datetime import datetime, timezone
    from uuid import uuid4

    from app.models.transaction_ops import TransactionCase
    from app.services.audit_service import log_event

    actor = admin_user[0]
    case = TransactionCase(
        tenant_id=actor.tenant_id,
        case_key=uuid4().hex,
        order_reference="R123456789",
        scope_json={"netsuite_account_id": "123456-sb1"},
        first_observed_at=datetime.now(timezone.utc),
        last_observed_at=datetime.now(timezone.utc),
        latest_report_json={},
    )
    db.add(case)
    await db.flush()
    payload = {
        **restriction(),
        "schema_version": 1,
        "account_id": "123456-sb1",
        "order_reference": case.order_reference,
    }
    payload.pop("audit_id")
    event = await log_event(
        db,
        actor.tenant_id,
        "transaction_ops",
        scope.ACTION,
        actor_id=actor.id,
        resource_type="transaction_case",
        resource_id=str(case.id),
        payload=payload,
    )
    actual = await scope.load(db, actor.tenant_id, case.id)
    assert actual["audit_id"] == str(event.id)
    assert actual["allowed_mutations"] == restriction()["allowed_mutations"]
    assert await scope.load(db, actor.tenant_id, uuid4()) is None
    assert await scope.load(db, admin_user_b[0].tenant_id, case.id) is None


@pytest.mark.parametrize("corruption", ["version", "account", "order", "allowed_size", "preserved_size", "conflict"])
async def test_malformed_persisted_scope_fails_closed(monkeypatch, corruption):
    from types import SimpleNamespace
    from uuid import uuid4

    value = {**restriction(), "schema_version": 1, "account_id": "account", "order_reference": "R123"}
    if corruption == "version":
        value["schema_version"] = 2
    elif corruption == "account":
        value["account_id"] = "other"
    elif corruption == "order":
        value["order_reference"] = "other"
    elif corruption == "allowed_size":
        value["allowed_mutations"] *= 21
    elif corruption == "preserved_size":
        value["preserve_records"] *= 21
    else:
        value["preserve_records"].append({"record_type": "creditmemo", "record_id": "40"})
    db = AsyncMock()
    db.scalar.side_effect = [
        SimpleNamespace(payload=value),
        SimpleNamespace(scope_json={"netsuite_account_id": "account"}, order_reference="R123"),
    ]
    monkeypatch.setattr(scope, "set_tenant_context", AsyncMock())
    with pytest.raises(ValueError, match="case_resolution_scope"):
        await scope.load(db, uuid4(), uuid4())


async def test_evidence_tool_filters_a_candidate_outside_human_case_scope(monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    from app.mcp.tools import transaction_ops_tools as mod
    from app.services.transaction_ops import (
        accounting_evidence,
        accounting_review,
        case_service,
        commercial_credits,
        resolution_assessment,
        tax_correction,
    )

    p, data = prepared()
    source, review, evidence, _ = data[:4]
    evidence.update(blockers=[], assessment={})
    db = AsyncMock()
    db.info = {}
    db.scalar.return_value = None
    tenant, actor_id, case_id = uuid4(), uuid4(), uuid4()
    case = SimpleNamespace(id=case_id, scope_json={}, latest_report_json={}, order_reference=source["number"])
    monkeypatch.setattr(mod, "_authorize", AsyncMock(return_value=(db, tenant, SimpleNamespace(id=actor_id))))
    monkeypatch.setattr(scope, "load", AsyncMock(return_value=restriction()))
    monkeypatch.setattr(case_service, "get_case", AsyncMock(return_value=case))
    monkeypatch.setattr(accounting_review, "accounting_context", AsyncMock(return_value=review))
    monkeypatch.setattr(accounting_evidence, "collect_accounting_evidence", AsyncMock(return_value=evidence))
    monkeypatch.setattr(tax_correction, "refresh_source", AsyncMock(return_value=source))
    monkeypatch.setattr(commercial_credits, "collect_commercial_credits", AsyncMock())
    monkeypatch.setattr(tax_correction, "candidate", lambda *a: {**p, "record_type": "salesorder", "record_id": "30"})
    monkeypatch.setattr(resolution_assessment, "reference_provenance", AsyncMock(return_value=[]))
    monkeypatch.setattr(resolution_assessment, "assess", lambda *a, **kw: {})
    monkeypatch.setattr("app.services.audit_service.log_event", AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    result = await mod.execute_accounting_evidence({"case_id": str(case_id)})
    assert result["success"] is True
    assert "outside_case_resolution_scope" in result["accounting_evidence"]["blockers"]
    assert not db.info.get("accounting_correction_candidate")
