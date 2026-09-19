"""Actual MCP proposal -> group card and approved credit -> posting recheck."""

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.chat.write_confirmation_service import build_confirmation_payload
from app.services.transaction_ops import accounting_credit_recheck as recheck
from app.services.transaction_ops import accounting_group, accounting_recheck, credit_api_correction
from app.services.transaction_ops.credit_reallocation import build_intent
from tests.test_accounting_recheck import approved_credit  # noqa: F401
from tests.test_credit_api_correction import schema
from tests.test_posting_balance import inputs


@pytest.fixture
async def mcp_credit(monkeypatch):
    source, review, evidence, support, report = inputs()
    source["id"] = "100"
    source["updated_at"] = datetime.now(timezone.utc).isoformat()
    support["credit"]["application_evidence"] = {"complete": True, "lines": [{"doc": "31", "amount": "440"}]}
    support["credit"]["createdFrom"] = {"id": support["invoice"]["id"]}
    review["native_mcp_connector_id"] = str(uuid4())
    intent = build_intent(str(uuid4()), str(uuid4()), source, review, evidence, support)
    reader = SimpleNamespace(request=AsyncMock(return_value=schema(intent["proposed_fields"])))

    @asynccontextmanager
    async def connection(*args, **kwargs):
        yield reader

    monkeypatch.setattr(credit_api_correction, "authenticated_reader", connection)
    p = await credit_api_correction.prepare(AsyncMock(), intent["tenant_id"], intent, evidence, None)
    assert "native_profile" not in p
    return p, (source, review, evidence, support), report


def member(p, session_id):
    name = f"ext__{p['connector_id'].replace('-', '')}__ns_updateRecord"
    card = build_confirmation_payload(
        mutation_type="update",
        record_type="creditmemo",
        tool_name=name,
        tool_input={"recordType": "creditmemo", "recordId": p["record_id"], "data": p["wire_record_json"]},
        session_id=str(session_id),
        current_record=p["before"],
    )
    card.accounting_review = p
    return {
        "case_id": p["case_id"],
        "order_reference": p["order_reference"],
        "confirmation_id": str(uuid4()),
        "card": {**card.model_dump(mode="json"), "accounting_group_child": True},
    }


async def test_real_mcp_credit_cards_build_and_validate_a_partial_group(mcp_credit):
    p, _, _ = mcp_credit
    session_id = uuid4()
    second = deepcopy(p)
    second.update(case_id=str(uuid4()), record_id="33", invoice_id="22", sales_order_id="12", order_reference="R124")
    members = [member(p, session_id), member(second, session_id), {"case_id": "unfinished", "reason": "deadline"}]
    card = accounting_group.build_group_card(members, {"group_id": "test", "scope": p["scope"]}, str(session_id))
    assert card.proposed_fields == {"eligible_orders": 2}
    assert len(card.accounting_group["treatment_batches"]) == 1
    assert len(card.accounting_group["investigation_batches"]) == 1
    assert accounting_group.validate_manifest(card.model_dump(mode="json"), str(session_id)) == members


@pytest.mark.parametrize(
    "field", ["connector_schema", "tax_account", "tax_item", "accounting_book", "period", "connector_id"]
)
async def test_mcp_credit_treatment_boundaries_remain_separate(mcp_credit, field):
    from app.services.transaction_ops.accounting_treatments import treatment_batches

    p, _, _ = mcp_credit
    other = deepcopy(p)
    other[field] = {"id": "other"} if isinstance(p[field], dict) else "other"
    assert len(treatment_batches([member(p, uuid4()), member(other, uuid4())])) == 2


def corrected(mcp_credit, now):
    p, current, report = deepcopy(mcp_credit)
    source, review, evidence, support = current
    support["credit"].update(subtotal="400", taxTotal="40", isTaxable=True)
    support["credit_gl"]["rows"][0]["debit"] = "400"
    support["credit_gl"]["rows"].append({"account": "13", "accountingbook": "1", "debit": "40"})
    for value in (support, evidence, report["refund_evidence"]["source"]):
        value["observed_at"] = now.isoformat()
    report["refund_evidence"]["target"] = {
        "complete": True,
        "record_ids": ["31"],
        "amount": "440",
        "currency": "USD",
        "order_reference": source["number"],
        "observed_at": now.isoformat(),
    }
    report.update(
        order_reference=source["number"],
        source={"record_id": source["id"], "observed_at": now.isoformat(), "authoritative": True},
        targets=[{"record_id": p["sales_order_id"], "observed_at": now.isoformat(), "authoritative": True}],
        lookup={"complete": True, "authoritative": True},
        balance={
            "status": "difference",
            "currency": "USD",
            "missing_metrics": [],
            "amounts": {
                "order_total": {"source": "1320", "target": "1760", "delta": "-440"},
                "tax": {"source": "120", "target": "160", "delta": "-40"},
                "refunds": {"source": "440", "target": "440", "delta": "0"},
            },
        },
    )
    return p, current, report


async def test_cm_parent_invoice_does_not_replace_the_sales_order_binding(mcp_credit):
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    run = SimpleNamespace(params_json={"verified_at": (now - timedelta(seconds=1)).isoformat()})
    assert accounting_recheck.report_in_scope(run, p, report, now)
    report["targets"][0]["record_id"] = p["invoice_id"]
    assert not accounting_recheck.report_in_scope(run, p, report, now)
    report["targets"][0]["record_id"] = p["sales_order_id"]
    p["support"]["invoice"]["createdFrom"]["id"] = "other"
    assert not accounting_recheck.report_in_scope(run, p, report, now)


async def test_posting_recheck_preserves_original_order_and_clears_only_proven_balance(mcp_credit):
    from app.services.transaction_ops.case_service import _cleared

    now = datetime.now(timezone.utc)
    p, current, report = corrected(mcp_credit, now)
    before = deepcopy((p, current, report))
    result = recheck.project(p, report, current, verified_at=now - timedelta(seconds=1), now=now)
    assert _cleared(result, now)
    assert result["balance"]["original_order_comparison"]["amounts"]["order_total"]["delta"] == "-440"
    assert result["balance"]["posting_reconciliation"]["sales_order_alignment"]["status"] == "observed_difference"
    assert (p, current, report) == before


async def test_penny_tax_difference_is_still_a_difference_after_credit(mcp_credit):
    now = datetime.now(timezone.utc)
    p, current, report = corrected(mcp_credit, now)
    current[3]["credit"].update(subtotal="400.01", taxTotal="39.99")
    current[3]["credit_gl"]["rows"][0]["debit"] = "400.01"
    current[3]["credit_gl"]["rows"][-1]["debit"] = "39.99"
    result = recheck.project(p, report, current, verified_at=now - timedelta(seconds=1), now=now)
    assert result["balance"]["status"] == "difference"
    assert result["balance"]["amounts"]["tax"]["delta"] == "-0.01"


@pytest.mark.parametrize(
    "change",
    ["stale", "future", "credit", "source", "invoice", "gl", "refund", "missing_refunds", "sales_order", "book"],
)
async def test_incomplete_or_changed_credit_evidence_cannot_clear_case(mcp_credit, change):
    now = datetime.now(timezone.utc)
    p, current, report = corrected(mcp_credit, now)
    source, _, evidence, support = current
    if change in {"stale", "future"}:
        support["observed_at"] = (now + timedelta(seconds=60 if change == "future" else -60)).isoformat()
    elif change in {"credit", "invoice", "refund"}:
        support[change]["id"] = "other"
    elif change == "source":
        source["total"] = "1"
    elif change == "sales_order":
        evidence["sections"]["sales_order"]["total"] = "1"
    elif change == "gl":
        support["credit_gl"]["complete"] = False
    elif change == "book":
        support["book"] = "2"
    else:
        report["refund_evidence"]["target"]["complete"] = False
    with pytest.raises(ValueError):
        recheck.project(p, report, current, verified_at=now - timedelta(seconds=1), now=now)


@pytest.mark.parametrize(
    "remaining_calls,failed,expected", [(56, False, "matched"), (55, False, "not_verified"), (56, True, "not_verified")]
)
async def test_readonly_recheck_reserves_budget_audits_and_never_reuses_failed_evidence(
    mcp_credit, monkeypatch, remaining_calls, failed, expected
):
    now = datetime.now(timezone.utc)
    p, current, report = corrected(mcp_credit, now)
    run = SimpleNamespace(
        id=uuid4(),
        lease_token=uuid4(),
        deadline_at=now + timedelta(minutes=5),
        api_calls_used=0,
        api_calls_held=0,
        max_api_calls=remaining_calls,
        params_json={"verified_at": (now - timedelta(seconds=1)).isoformat(), "approval_message_id": str(uuid4())},
    )
    fresh = AsyncMock(side_effect=ValueError("unavailable")) if failed else AsyncMock(return_value=current)
    audit = AsyncMock()
    monkeypatch.setattr(credit_api_correction, "fresh", fresh)
    monkeypatch.setattr(recheck.state, "_audit", audit)
    monkeypatch.setattr(recheck.state, "_commit", AsyncMock())
    monkeypatch.setattr(recheck.state, "get_run", AsyncMock(return_value=run))
    monkeypatch.setattr(recheck.state, "_lease", lambda *args: None)
    db = AsyncMock()

    @asynccontextmanager
    async def read_session(caller_db):
        yield db  # the provider read runs on its own session; here the same mock stands in for both

    monkeypatch.setattr(recheck, "_read_session", read_session)
    monkeypatch.setattr(recheck, "set_tenant_context", AsyncMock())
    result = await recheck.reconcile(db, p["tenant_id"], run, p, report)
    assert result["balance"]["status"] == expected
    assert fresh.await_count == (remaining_calls >= recheck.READ_CALLS)
    assert audit.call_args.kwargs["payload"]["financial_writes"] == 0


@pytest.mark.parametrize(
    "change,expected", [(None, "succeeded"), ("penny", "difference"), ("unavailable", "unverified")]
)
async def test_full_runner_persists_credit_recheck_case_and_audit_without_writes(
    db,
    approved_credit,  # noqa: F811
    mcp_credit,
    monkeypatch,
    change,
    expected,
):
    import sys

    from sqlalchemy import select

    from app.models.audit import AuditEvent
    from app.services.transaction_ops import case_service, state_service
    from app.services.transaction_ops.runner import run_investigation

    finish = state_service.finish_run

    async def checked_finish(*args, **kwargs):
        if args[3] == "error" and sys.exception():
            raise sys.exception()
        return await finish(*args, **kwargs)

    monkeypatch.setattr(state_service, "finish_run", checked_finish)

    actor, config, case, message, source_envelope, target = approved_credit
    now = datetime.now(timezone.utc)
    p, current, report = corrected(mcp_credit, now)
    source, review, evidence, support = current
    p.update(
        tenant_id=str(actor.tenant_id),
        config_id=str(config.id),
        case_id=str(case.id),
        scope=case.scope_json,
        order_reference=case.order_reference,
    )
    review["scope"] = case.scope_json
    source["number"] = p["source"]["number"] = case.order_reference
    evidence["sections"]["sales_order"]["tranId"] = p["protected_sales_order"]["tranId"] = case.order_reference
    support["credit"]["custbody_fw_order_number"] = p["support"]["credit"]["custbody_fw_order_number"] = (
        case.order_reference
    )
    source_envelope["orders"][0] = {
        **source,
        "business_entity": "Framework Inc",
        "included_tax_total": "0",
        "additional_tax_total": "120",
    }
    target["orders"][0].update(record_id=p["sales_order_id"], order_reference=case.order_reference)
    target["orders"][0]["header"].update(id=p["sales_order_id"], total="1760", taxTotal="160")
    for side in report["refund_evidence"].values():
        side["order_reference"] = case.order_reference
    if change == "penny":
        support["credit"].update(subtotal="400.01", taxTotal="39.99")
        support["credit_gl"]["rows"][0]["debit"] = "400.01"
        support["credit_gl"]["rows"][-1]["debit"] = "39.99"
    so = deepcopy(message.structured_output)
    so["accounting_review"] = p
    message.structured_output = so
    config.max_api_calls = 100
    await db.flush()
    run = await accounting_recheck.queue(db, actor.tenant_id, message, actor.id, now=now - timedelta(seconds=1))
    await db.commit()
    source_envelope["read_at"] = target["observed_at"] = now.isoformat()
    fresh = (
        AsyncMock(side_effect=ValueError("unavailable")) if change == "unavailable" else AsyncMock(return_value=current)
    )
    monkeypatch.setattr(credit_api_correction, "fresh", fresh)
    guard = AsyncMock(side_effect=AssertionError("No proposals or financial writes in recheck"))
    result = await run_investigation(
        db,
        actor.tenant_id,
        run.id,
        _clock=lambda: now,
        _enabled=AsyncMock(return_value=True),
        _source_reader=AsyncMock(return_value=source_envelope),
        _target_reader=AsyncMock(return_value=target),
        _source_refunds_reader=AsyncMock(return_value=report["refund_evidence"]["source"]),
        _target_refunds_reader=AsyncMock(return_value=report["refund_evidence"]["target"]),
        _guard_reader=guard,
        _celigo_reader=guard,
        _create_reader=guard,
    )
    assert result["termination_reason"] == "done"
    assert run.progress_json["settlement"]["status"] == expected
    assert run.progress_json["settlement"]["cash_settlement"] == "not_verified"
    # Every reservation the run makes, less the data share of the two metered reads that
    # these fake readers never send: 7 for the order read and MAX_REFUND_CALLS for the
    # refund read. Each keeps its OAuth maintenance share; what remains is 73 - 7.
    # Spelled out as a literal this silently became wrong the moment the budget changed.
    assert run.api_calls_used == 73 - 7
    fresh.assert_awaited_once()  # No extra subledger reads at the partial refund checkpoint.
    guard.assert_not_awaited()
    assert not await state_service.list_proposals(db, actor.tenant_id, run_id=run.id)
    updated = await case_service.get_case(db, actor.tenant_id, case.id)
    assert updated.status == ("reconciled" if expected == "succeeded" else "open")
    audits = list(
        await db.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == actor.tenant_id,
                AuditEvent.resource_id == str(run.id),
                AuditEvent.action == "transaction_ops.accounting_recheck.posting_observed",
            )
        )
    )
    assert len(audits) == 1 and audits[0].payload["financial_writes"] == 0


async def test_mixed_legacy_and_mcp_members_build_and_validate_one_group_card(mcp_credit):
    """The group crash was an MCP proposal meeting a native-only branch; a mixed group must build and sign."""
    from tests.test_accounting_group import group_fixture

    p, _, _ = mcp_credit
    so, session = group_fixture(2)
    legacy = so["accounting_group"]["members"]
    first, second = [m["card"]["accounting_review"] for m in legacy]
    second.update({key: first[key] for key in ("connector_id", "connection_id")})
    members = [*legacy, member(p, session.id), {"case_id": "unfinished", "reason": "deadline"}]

    card = accounting_group.build_group_card(members, {"group_id": "mixed", "scope": p["scope"]}, str(session.id))

    assert card.proposed_fields == {"eligible_orders": 3}
    batches = {b["treatment"]["kind"]: b for b in card.accounting_group["treatment_batches"]}
    assert set(batches) == {"credit_tax_reallocation", "invoice_tax"}
    assert batches["credit_tax_reallocation"]["treatment"]["execution_transport"] == "mcp_record_api"
    assert "connector_schema" in batches["credit_tax_reallocation"]["treatment"]["profile"]
    assert batches["invoice_tax"]["treatment"]["execution_transport"] is None
    assert sorted(batches["invoice_tax"]["case_ids"]) == sorted(m["case_id"] for m in legacy)
    assert len(card.accounting_group["investigation_batches"]) == 1
    assert accounting_group.validate_manifest(card.model_dump(mode="json"), str(session.id)) == members
