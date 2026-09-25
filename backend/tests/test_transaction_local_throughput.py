"""Throughput shortcuts retain evidence, ownership and restart boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.transaction_ops import TransactionCaseObservation, TransactionFinding
from app.schemas.transaction_runs import ProgressUpdate
from app.services.transaction_ops import refund_reader, staged_netsuite
from app.services.transaction_ops import state_service as state_service
from app.services.transaction_ops.runner import run_investigation
from tests.test_transaction_cases import report
from tests.test_transaction_ops_runner import NOW, REF, State, missing_target, source_order
from tests.test_transaction_ops_state_db import setup_state  # noqa: F401


@pytest.mark.parametrize(
    "mode",
    [
        "cached",
        "source_miss",
        "batch_miss",
        "budget",
        "commit_failure",
        "partial_failure",
        "actions",
        "actions_abstain",
    ],
)
async def test_cached_refunds_skip_only_redundant_partial_commit(monkeypatch, mode):
    state = State(window=True)
    state.run.config_snapshot["mapping_json"]["solidus_refund_step_id"] = str(uuid4())
    if mode in {"actions", "actions_abstain"}:
        state.run.config_snapshot["mapping_json"]["action_mode"] = "propose_actions"
    state.run.progress_json = {
        "pending_refs": [REF],
        "scan_complete": True,
        "refund_scan_complete": True,
        "dependency_scan_complete": True,
    }
    state.record_finding = AsyncMock(wraps=state.record_finding)
    if mode in {"commit_failure", "partial_failure"}:
        state.record_finding.side_effect = RuntimeError("failed commit")
    refund = {"complete": True, "currency": "USD", "amount": "0", "observed_at": NOW.isoformat()}
    batch = AsyncMock()
    batch.get.return_value = None if mode in {"source_miss", "budget", "partial_failure"} else refund
    monkeypatch.setattr(refund_reader, "RefundBatch", lambda: batch)

    async def fresh_source(*args):
        # The partial finding exists before spending on or issuing this read.
        assert state.record_finding.call_args.kwargs["final"] is False
        return refund

    single = AsyncMock(side_effect=fresh_source)
    monkeypatch.setattr(refund_reader, "read_solidus_refunds", single)

    async def native_refund(*args, before_fetch=None, **kwargs):
        if mode == "batch_miss":
            await before_fetch()
            assert state.record_finding.call_args.kwargs["final"] is False
        return refund

    native = SimpleNamespace(
        order=AsyncMock(return_value=missing_target()), refund=AsyncMock(side_effect=native_refund)
    )
    monkeypatch.setattr(staged_netsuite, "StagedNetSuite", lambda *a, **kw: native)
    if mode == "budget":
        state.budget = 2  # Source order uses all remaining calls.
    source = source_order()
    if mode == "actions_abstain":
        source["orders"][0]["state"] = "canceled"
        state.propose = AsyncMock()
    result = await run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _source_reader=AsyncMock(return_value=source),
        _order_mirror=AsyncMock(),
        _enabled=AsyncMock(return_value=True),
        _clock=lambda: NOW,
    )
    partials = [call for call in state.record_finding.call_args_list if call.kwargs.get("final") is False]
    if mode in {"budget", "commit_failure", "partial_failure"}:
        assert result["termination_reason"] == ("budget" if mode == "budget" else "error")
        assert result["processed"] == 0 and state.run.progress_json["pending_refs"] == [REF]
        if mode == "budget":
            assert len(partials) == 1 and REF in state.reports
            single.assert_not_awaited()
        if mode == "partial_failure":
            single.assert_not_awaited()
            assert state.record_finding.await_count == 1 and not state.reports
        return
    assert result["termination_reason"] == "done" and result["processed"] == 1
    if mode == "actions_abstain":
        state.propose.assert_not_awaited()
        batch.get.assert_awaited_once()
    assert len(partials) == (0 if mode in {"cached", "actions_abstain"} else 1)
    assert state.record_finding.call_args.kwargs["checkpoint"].progress_json["pending_refs"] == []
    assert state.reports[REF]["refund_evidence"]["source"]["observed_at"] == NOW.isoformat()


async def test_finding_upsert_retains_id_finality_history_and_atomic_cursor(db, setup_state):  # noqa: F811
    actor, _, run = setup_state
    token = await state_service.claim_run(db, actor.tenant_id, run.id)
    reference = "R123456789"
    first = await state_service.record_finding(
        db,
        actor.tenant_id,
        run.id,
        reference,
        report(),
        lease_token=token,
        final=False,
    )
    identifier = first.id
    assert first.report_json["_observation"]["final"] is False
    assert await db.scalar(select(func.count()).select_from(TransactionCaseObservation)) == 0
    final = await state_service.record_finding(
        db,
        actor.tenant_id,
        run.id,
        reference,
        report(),
        lease_token=token,
        checkpoint=ProgressUpdate(progress_json={"pending_refs": [], "processed": 1}),
    )
    assert final.id == identifier and final.report_json["_observation"]["final"] is True
    assert "case_id" in final.report_json
    assert (await state_service.get_run(db, actor.tenant_id, run.id)).progress_json["processed"] == 1
    assert await db.scalar(select(func.count()).select_from(TransactionFinding)) == 1
    assert await db.scalar(select(func.count()).select_from(TransactionCaseObservation)) == 1


async def test_failed_case_persistence_rolls_back_finding_and_cursor(db, setup_state, monkeypatch):  # noqa: F811
    from app.services.transaction_ops import case_service

    actor, _, run = setup_state
    tenant, run_id = actor.tenant_id, run.id
    token = await state_service.claim_run(db, tenant, run_id)
    monkeypatch.setattr(case_service, "observe_finding", AsyncMock(side_effect=RuntimeError("case write failed")))
    with pytest.raises(RuntimeError, match="case write failed"):
        await state_service.record_finding(
            db,
            tenant,
            run_id,
            "R123456789",
            report(),
            lease_token=token,
            checkpoint=ProgressUpdate(progress_json={"pending_refs": [], "processed": 1}),
        )
    await db.rollback()
    assert await db.scalar(select(func.count()).select_from(TransactionFinding)) == 0
    assert not (await state_service.get_run(db, tenant, run_id)).progress_json.get("processed")
