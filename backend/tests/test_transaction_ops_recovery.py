from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from app.models.transaction_ops import TransactionRun
from app.services.transaction_ops import recovery as mod
from app.services.transaction_ops import state_service as state
from tests import test_transaction_ops_executor as execution_fixtures
from tests.test_transaction_ops_executor import execute, operation

execution_case = execution_fixtures.execution_case


@pytest.fixture
async def unknown_case(db, execution_case):
    case = execution_case.case
    case.read_target.side_effect = [execution_case.before, execution_case.before]
    case.read_guard.side_effect = [case.guard, case.guard]
    assert (await execute(db, execution_case))["status"] == "unknown"
    return execution_case


def mock_recovery(monkeypatch, case, *, unchanged=False):
    monkeypatch.setattr(mod, "read_framework_order", AsyncMock(return_value=case.case.source))
    monkeypatch.setattr(mod, "read_netsuite_order", AsyncMock(return_value=case.before if unchanged else case.after))
    monkeypatch.setattr(
        mod, "read_guard_snapshot", AsyncMock(return_value=case.case.guard if unchanged else case.after_guard)
    )


async def test_unknown_operation_gets_one_durable_read_only_budget_without_schedule_opt_in(
    db, unknown_case, monkeypatch
):
    row = await operation(db, unknown_case)
    assert row.status == "unknown"
    mock_recovery(monkeypatch, unknown_case)
    result = await mod.recover_operation(db, unknown_case.actor.tenant_id, row.id)
    assert result["status"] == "verified"
    assert (await operation(db, unknown_case)).result_json["reconciled"] is True
    runs = (
        (
            await db.execute(
                select(TransactionRun).where(
                    TransactionRun.tenant_id == unknown_case.actor.tenant_id, TransactionRun.origin == "recovery"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(runs) == 1 and runs[0].status == "finished"
    assert runs[0].api_calls_used == 16 and runs[0].max_api_calls == 32 and runs[0].orders_used == 1
    assert (await db.execute(text("SELECT current_setting('app.current_tenant_id',true)"))).scalar_one() == str(
        unknown_case.actor.tenant_id
    )
    unknown_case.case.dispatch.assert_awaited_once()
    await mod.recover_operation(db, unknown_case.actor.tenant_id, row.id)
    mod.read_framework_order.assert_awaited_once()


async def test_negative_recovery_does_not_resend_or_reset_its_read_budget(db, unknown_case, monkeypatch):
    row = await operation(db, unknown_case)
    mock_recovery(monkeypatch, unknown_case, unchanged=True)
    result = await mod.recover_operation(db, unknown_case.actor.tenant_id, row.id)
    assert result["status"] == "unknown"
    assert (await operation(db, unknown_case)).result_json["recovery"]["termination_reason"] == "stall"
    await mod.recover_operation(db, unknown_case.actor.tenant_id, row.id)
    mod.read_framework_order.assert_awaited_once()
    unknown_case.case.dispatch.assert_awaited_once()


async def test_running_operation_is_not_recovered_before_its_deadline(db, execution_case, monkeypatch):
    case = execution_case
    claim = await state.claim_approved_operation(
        db, case.actor.tenant_id, case.proposal.id, expected_evidence_fingerprint=case.proposal.evidence_fingerprint
    )
    row = await operation(db, case)
    mock_recovery(monkeypatch, case)
    assert (await mod.recover_operation(db, case.actor.tenant_id, claim.operation_id))["status"] == "executing"
    mod.read_framework_order.assert_not_awaited()
    result = await mod.recover_operation(
        db, case.actor.tenant_id, claim.operation_id, _clock=lambda: row.deadline_at + timedelta(seconds=1)
    )
    assert result["status"] == "failed"
    mod.read_framework_order.assert_not_awaited()


async def test_recovery_never_accepts_changed_source_as_the_approved_outcome(db, unknown_case, monkeypatch):
    row = await operation(db, unknown_case)
    mock_recovery(monkeypatch, unknown_case)
    changed = deepcopy(unknown_case.case.source)
    changed["orders"][0]["total"] = "101"
    mod.read_framework_order.return_value = changed
    assert (await mod.recover_operation(db, unknown_case.actor.tenant_id, row.id))["status"] == "unknown"
    unknown_case.case.dispatch.assert_awaited_once()


async def test_audit_failure_cannot_commit_a_finished_recovery_without_its_outcome(db, unknown_case, monkeypatch):
    row = await operation(db, unknown_case)
    mock_recovery(monkeypatch, unknown_case)
    run = await state.create_operation_recovery(db, unknown_case.actor.tenant_id, row.id)
    tenant_id, run_id, proposal_id = unknown_case.actor.tenant_id, run.id, unknown_case.proposal.id
    original = state._audit

    async def audit(*args, **kwargs):
        if args[2] == "operation.recovery.complete":
            raise RuntimeError("audit unavailable")
        return await original(*args, **kwargs)

    monkeypatch.setattr(state, "_audit", audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await mod.recover_operation(db, unknown_case.actor.tenant_id, row.id)
    await db.rollback()
    assert (await state.get_run(db, tenant_id, run_id)).status == "running"
    from app.models.transaction_ops import TransactionOperation

    assert (
        await db.execute(select(TransactionOperation.status).where(TransactionOperation.proposal_id == proposal_id))
    ).scalar_one() == "unknown"


async def test_recovery_run_delivered_to_generic_queue_still_uses_only_recovery(db, unknown_case, monkeypatch):
    row = await operation(db, unknown_case)
    run = await state.create_operation_recovery(db, unknown_case.actor.tenant_id, row.id)
    mock_recovery(monkeypatch, unknown_case)
    from app.services.transaction_ops.runner import run_investigation

    forbidden = AsyncMock(side_effect=AssertionError("recovery must not enter investigation planning"))
    result = await run_investigation(db, unknown_case.actor.tenant_id, run.id, _source_reader=forbidden)
    assert result["status"] == "verified"
    forbidden.assert_not_awaited()
    unknown_case.case.dispatch.assert_awaited_once()
