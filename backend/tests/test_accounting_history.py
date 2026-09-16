import json
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.transaction_ops import TransactionFinding
from app.services.chat.write_confirmation_service import mint_confirmation_token
from app.services.transaction_ops import accounting_history, accounting_recheck
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.accounting_recovery import execution_claim
from app.services.transaction_ops.resolution_history import _issue
from app.services.transaction_ops.runner import run_investigation
from tests.test_accounting_approval_flow import inputs
from tests.test_accounting_recheck import approved_credit  # noqa: F401


@pytest.fixture
async def reconciled_credit(db, approved_credit):  # noqa: F811
    actor, config, case, message, source, target = approved_credit
    so = deepcopy(message.structured_output)
    p = so["accounting_review"]
    p["connection_id"] = str(config.netsuite_connection_id)
    p["resolution_assessment"] = {"comparison_signature": _issue(case.latest_report_json)}
    tool_name, tool_input = inputs(p)
    so.update(tool_name=tool_name, tool_input=tool_input, editable_slots=[])
    so["confirmation_token"] = mint_confirmation_token(
        tool_name=tool_name, tool_input=tool_input, session_id=str(message.session_id), editable_slots=[]
    )
    message.structured_output = execution_claim(so, message.id, actor.id, {}, now=datetime.now(timezone.utc))
    await db.flush()
    run = await accounting_recheck.queue(db, actor.tenant_id, message, actor.id, now=datetime.now(timezone.utc))
    await db.commit()
    observed = datetime.now(timezone.utc)
    source["read_at"] = target["observed_at"] = observed.isoformat()
    target["orders"][0]["header"].update(total="120.00", taxTotal="20.00")
    refund = {"complete": True, "amount": "0.00", "currency": "USD", "order_reference": case.order_reference}
    guard = AsyncMock(side_effect=AssertionError("History recheck must never write"))
    await run_investigation(
        db,
        actor.tenant_id,
        run.id,
        _clock=lambda: observed,
        _enabled=AsyncMock(return_value=True),
        _source_reader=AsyncMock(return_value=source),
        _target_reader=AsyncMock(return_value=target),
        _source_refunds_reader=AsyncMock(return_value=refund),
        _target_refunds_reader=AsyncMock(return_value=refund),
        _guard_reader=guard,
        _celigo_reader=guard,
        _create_reader=guard,
    )
    guard.assert_not_awaited()
    await db.refresh(run)
    await db.refresh(message)
    await db.refresh(case)
    finding = await db.scalar(
        select(TransactionFinding).where(
            TransactionFinding.tenant_id == actor.tenant_id, TransactionFinding.run_id == run.id
        )
    )
    assert run.progress_json["settlement"]["status"] == "succeeded"
    return actor, config, case, message, run, finding


async def test_real_recheck_chain_and_original_approver_are_visible(db, reconciled_credit):
    actor, _, case, message, run, finding = reconciled_credit
    assert accounting_history.verified_resolution(message, run, finding, case)
    result = await accounting_history.history(db, actor.tenant_id, case, run)
    row = result["resolutions"][0]
    assert row["verified_example"]
    assert row["approved_by"] == str(actor.id)
    assert row["approval_message_id"] == str(message.id)
    assert row["reconciliation_status"] == "succeeded"
    assert row["cash_settlement"] == "not_verified"
    assert row["requires_new_human_approval"]
    assert "tool_input" not in row and "confirmation_token" not in row


@pytest.mark.parametrize(
    "variant",
    [
        "unsigned",
        "altered_claim",
        "wrong_approver",
        "wrong_finding",
        "wrong_scope",
        "unverified_native",
        "recheck_pending",
        "reopened",
        "changed_source",
        "refund_gap",
    ],
)
async def test_incomplete_or_changed_chains_are_not_successful_examples(db, reconciled_credit, variant):
    _, _, case, message, run, finding = reconciled_credit
    from types import SimpleNamespace

    m = SimpleNamespace(
        id=message.id,
        session_id=message.session_id,
        tenant_id=message.tenant_id,
        role=message.role,
        structured_output=deepcopy(message.structured_output),
    )
    r = SimpleNamespace(
        **{
            k: deepcopy(getattr(run, k))
            for k in (
                "id",
                "tenant_id",
                "config_id",
                "work_key",
                "origin",
                "status",
                "termination_reason",
                "params_json",
                "progress_json",
                "initiated_by",
            )
        }
    )
    f = SimpleNamespace(
        **{k: deepcopy(getattr(finding, k)) for k in ("id", "tenant_id", "run_id", "order_reference", "report_json")}
    )
    c = SimpleNamespace(
        **{
            k: deepcopy(getattr(case, k))
            for k in ("id", "tenant_id", "scope_json", "order_reference", "status", "latest_report_json")
        }
    )
    if variant == "unsigned":
        m.structured_output["confirmation_token"] = "invalid"
    elif variant == "altered_claim":
        m.structured_output["accounting_execution"]["evidence_digest"] = "changed"
    elif variant == "wrong_approver":
        r.params_json["approved_by"] = str(uuid4())
    elif variant == "wrong_finding":
        f.id = uuid4()
    elif variant == "wrong_scope":
        c.scope_json["subsidiary_id"] = "other"
    elif variant == "unverified_native":
        m.structured_output["accounting_verification"]["status"] = "not_verified"
    elif variant == "recheck_pending":
        r.status = "pending"
    elif variant == "reopened":
        c.status = "open"
    elif variant == "changed_source":
        c.latest_report_json["source"]["total"] = "999"
    elif variant == "refund_gap":
        f.report_json["balance"]["missing_metrics"] = ["refunds"]
    assert not accounting_history.verified_resolution(m, r, f, c)


async def test_history_rejects_foreign_scope(db, reconciled_credit, tenant_b):
    _, _, case, _, run, _ = reconciled_credit
    with pytest.raises(state.StateError, match="scope_mismatch"):
        await accounting_history.history(db, tenant_b.id, case, run)


async def test_real_tool_dispatch_delivers_verified_history_to_agent(db, reconciled_credit, admin_user_b, monkeypatch):
    from app.services.chat.tools import execute_tool_call
    from app.services.transaction_ops.chat_evidence import condense_status
    from tests.conftest import enable_feature_flag

    actor, _, case, message, _, _ = reconciled_credit
    foreign = admin_user_b[0]
    for user in (actor, foreign):
        for flag in ("celigo", "reconciliation"):
            await enable_feature_flag(db, user.tenant_id, flag)
    monkeypatch.setattr("app.mcp.governance.check_rate_limit", lambda *args: True)

    async def dispatch(user):
        return json.loads(
            await execute_tool_call(
                "transaction_ops_status", {"case_id": str(case.id)}, user.tenant_id, user.id, "history-proof", db
            )
        )

    result = await dispatch(actor)
    assert result["success"], result
    context = json.loads(condense_status(result))
    row = context["accounting_resolution_history"][0]
    assert row["approval_message_id"] == str(message.id)
    assert row["approved_by"] == str(actor.id) and row["verified_example"]
    assert "new exact approval" in context["accounting_resolution_usage"]
    denied = await dispatch(foreign)
    assert denied["error"] == "not_found"
    assert "accounting_resolution_history" not in denied


@pytest.mark.parametrize("variant", ["same_scope", "profile_changed", "mapping_changed", "issue_changed", "reopened"])
async def test_related_examples_require_compatible_current_policy_and_verified_case(
    db, reconciled_credit, monkeypatch, variant
):
    from app.schemas.transaction_runs import ConfigCreate, RunCreate
    from app.services.transaction_ops import accounting_profiles, case_service

    actor, config, old_case, message, checked_run, _ = reconciled_credit
    original = await db.scalar(
        select(TransactionFinding).where(
            TransactionFinding.tenant_id == actor.tenant_id,
            TransactionFinding.run_id != checked_run.id,
            TransactionFinding.order_reference == old_case.order_reference,
        )
    )
    report = deepcopy(original.report_json)
    ref = "R222222222"
    report.pop("case_id", None)
    report["order_reference"] = ref
    for snapshot in (report["source"], *report["targets"]):
        snapshot["order_reference"] = ref
    if variant == "issue_changed":
        report["balance"]["currency"] = "EUR"
    if variant == "mapping_changed":
        values = {key: getattr(config, key) for key in ConfigCreate.model_fields}
        values["mapping_json"] = {**config.mapping_json, "action_mode": "detect_only"}
        config = await state.create_config(db, actor.tenant_id, ConfigCreate(**values), actor=actor)
    if variant == "reopened":
        old_case.status = "open"
    await db.flush()
    run = await state.create_run(
        db, actor.tenant_id, config.id, RunCreate(evaluation_key=str(uuid4()), order_references=[ref]), actor=actor
    )
    token = await state.claim_run(db, actor.tenant_id, run.id)
    finding = await state.record_finding(db, actor.tenant_id, run.id, ref, report, lease_token=token)
    await state.finish_run(db, actor.tenant_id, run.id, "done", lease_token=token)
    case = await case_service.get_case(db, actor.tenant_id, finding.report_json["case_id"])
    profile = deepcopy(message.structured_output["accounting_review"]["profile"])
    if variant == "profile_changed":
        profile["currency"] = "EUR"
    monkeypatch.setattr(accounting_profiles, "sales_credit_profile", AsyncMock(return_value=profile))
    result = await accounting_history.history(db, actor.tenant_id, case, run)
    assert not result["resolutions"]
    assert len(result["examples"]) == (1 if variant == "same_scope" else 0)
    if result["examples"]:
        assert result["examples"][0]["approved_by"] == str(actor.id)
        assert result["examples"][0]["requires_new_human_approval"]
