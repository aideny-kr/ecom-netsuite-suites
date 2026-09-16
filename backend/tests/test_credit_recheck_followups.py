"""Follow-ups from the review of the credit-recheck fix (PR #262, 6fb2d316).

1. Every recheck exit writes the posting_observed audit and rechecks the lease. Failures
   of the evidence, including transport failures the previous except-tuple missed and
   the runner's own {"complete": False} fallback shapes, degrade to not_verified. A
   genuine defect is audited, committed, and then surfaced, never reclassified.
2. Interim findings of a recheck run stay bound to the approval; only the expensive
   subledger recheck waits for the final write.
3. The MCP recheck ceiling is derived from the read budget it exists to afford.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.models.transaction_ops import TransactionFinding
from app.services.transaction_ops import accounting_credit_recheck as recheck
from app.services.transaction_ops import accounting_recheck, credit_api_correction, state_service
from app.services.transaction_ops.source_reader import SourceReadError
from tests.test_accounting_recheck import approved_credit  # noqa: F401
from tests.test_accounting_release_regressions import corrected, mcp_credit  # noqa: F401

UNAVAILABLE = {"complete": False, "reason": "target_refunds_unavailable"}  # runner.py's fallback shape


def _run(now, *, max_api_calls=200):
    return SimpleNamespace(
        id=uuid4(),
        lease_token=uuid4(),
        deadline_at=now + timedelta(minutes=5),
        api_calls_used=0,
        max_api_calls=max_api_calls,
        params_json={"verified_at": (now - timedelta(seconds=1)).isoformat(), "approval_message_id": str(uuid4())},
    )


def _patch_state(monkeypatch, run):
    audit, commit, get_run, lease = AsyncMock(), AsyncMock(), AsyncMock(return_value=run), Mock()
    monkeypatch.setattr(recheck.state, "_audit", audit)
    monkeypatch.setattr(recheck.state, "_commit", commit)
    monkeypatch.setattr(recheck.state, "get_run", get_run)
    monkeypatch.setattr(recheck.state, "_lease", lease)
    return audit, commit, get_run, lease


def _posted(audit):
    return [c for c in audit.call_args_list if c.args[2] == "accounting_recheck.posting_observed"]


@pytest.mark.parametrize(
    "exc",
    [
        SourceReadError("source_transport_failed"),
        httpx.ConnectError("connection reset"),
        TimeoutError(),
        KeyError("refund_evidence"),
        TypeError("'NoneType' object is not subscriptable"),
    ],
    ids=["source_reader", "httpx", "timeout", "missing_key", "wrong_shape"],
)
async def test_evidence_failure_during_recheck_is_not_verified_with_audit_and_lease_recheck(
    mcp_credit,  # noqa: F811
    monkeypatch,
    exc,
):
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    run = _run(now)
    audit, commit, get_run, lease = _patch_state(monkeypatch, run)
    monkeypatch.setattr(credit_api_correction, "fresh", AsyncMock(side_effect=exc))

    result = await recheck.reconcile(AsyncMock(), p["tenant_id"], run, p, report)

    assert result["balance"]["status"] == "not_verified"
    assert result["balance"]["reason"] == "credit_recheck_evidence_unavailable"
    assert result["balance"]["amounts"] == report["balance"]["amounts"]
    posted = _posted(audit)
    assert len(posted) == 1
    assert posted[0].kwargs["payload"]["financial_writes"] == 0
    assert posted[0].kwargs["payload"]["balance"]["status"] == "not_verified"
    get_run.assert_awaited_once()
    lease.assert_called_once()
    assert commit.await_count == 1  # the reservation; the caller commits the rest
    assert run.api_calls_used == recheck.READ_CALLS  # a failed read still consumed its reservation


async def test_defect_during_recheck_is_audited_committed_then_surfaced(mcp_credit, monkeypatch):  # noqa: F811
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    run = _run(now)
    audit, commit, get_run, lease = _patch_state(monkeypatch, run)
    monkeypatch.setattr(credit_api_correction, "fresh", AsyncMock(side_effect=RuntimeError("session poisoned")))

    with pytest.raises(RuntimeError):
        await recheck.reconcile(AsyncMock(), p["tenant_id"], run, p, report)

    posted = _posted(audit)
    assert len(posted) == 1
    payload = posted[0].kwargs["payload"]
    assert payload["financial_writes"] == 0
    assert payload["balance"]["status"] == "not_verified"
    assert payload["balance"]["reason"] == "credit_recheck_internal_error"
    assert payload["error_type"] == "RuntimeError"
    get_run.assert_awaited_once()
    lease.assert_called_once()
    # Reservation commit, then the commit that makes the audit survive the raise.
    assert commit.await_count == 2
    assert audit.await_args_list[-1].args[2] == "accounting_recheck.posting_observed"


@pytest.mark.parametrize(
    "change",
    ["no_refund_evidence", "no_amounts", "target_unavailable", "source_unavailable", "support_unstamped"],
)
async def test_incomplete_report_is_a_verification_failure_not_a_crash(mcp_credit, change):  # noqa: F811
    now = datetime.now(timezone.utc)
    p, current, report = corrected(mcp_credit, now)
    if change == "no_refund_evidence":
        del report["refund_evidence"]
    elif change == "no_amounts":
        del report["balance"]["amounts"]
    elif change == "target_unavailable":
        report["refund_evidence"]["target"] = dict(UNAVAILABLE)
    elif change == "source_unavailable":
        report["refund_evidence"]["source"] = {"complete": False, "reason": "source_refunds_unavailable"}
    else:
        del current[3]["observed_at"]
    with pytest.raises(ValueError):
        recheck.project(p, report, current, verified_at=now - timedelta(seconds=1), now=now)


async def test_runner_fallback_refund_shape_reaches_not_verified_through_reconcile(mcp_credit, monkeypatch):  # noqa: F811
    now = datetime.now(timezone.utc)
    p, current, report = corrected(mcp_credit, now)
    report["refund_evidence"]["target"] = dict(UNAVAILABLE)
    run = _run(now)
    audit, commit, _, _ = _patch_state(monkeypatch, run)
    monkeypatch.setattr(credit_api_correction, "fresh", AsyncMock(return_value=current))

    result = await recheck.reconcile(AsyncMock(), p["tenant_id"], run, p, report)

    assert result["balance"]["status"] == "not_verified"
    assert result["balance"]["reason"] == "credit_recheck_incomplete_evidence"  # project()'s own reason survives
    assert len(_posted(audit)) == 1
    assert commit.await_count == 1


async def test_interim_bound_report_annotates_scope_without_the_expensive_recheck(mcp_credit, monkeypatch):  # noqa: F811
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    monkeypatch.setattr(accounting_recheck, "approval_for_run", AsyncMock(return_value=(SimpleNamespace(), p)))
    spy = AsyncMock(return_value={"reconciled": True})
    monkeypatch.setattr(recheck, "reconcile", spy)
    run = SimpleNamespace(params_json={"verified_at": (now - timedelta(seconds=1)).isoformat()})

    interim = await accounting_recheck.bound_report(
        AsyncMock(), p["tenant_id"], run, report, now=now, subledger_recheck=False
    )
    assert interim == report
    spy.assert_not_awaited()

    final = await accounting_recheck.bound_report(
        AsyncMock(), p["tenant_id"], run, report, now=now, subledger_recheck=True
    )
    assert final == {"reconciled": True}
    spy.assert_awaited_once()

    out_of_scope = deepcopy(report)
    out_of_scope["targets"][0]["record_id"] = p["invoice_id"]
    bound = await accounting_recheck.bound_report(
        AsyncMock(), p["tenant_id"], run, out_of_scope, now=now, subledger_recheck=False
    )
    assert bound["balance"]["status"] == "not_verified"
    assert bound["balance"]["reason"] == "accounting_recheck_identity_or_freshness_unverified"
    spy.assert_awaited_once()  # still only the final call


async def test_interim_finding_of_a_recheck_run_is_bound_to_the_approval(
    db,
    approved_credit,  # noqa: F811
    mcp_credit,  # noqa: F811
    monkeypatch,
):
    actor, config, case, message, _, _ = approved_credit
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    p.update(
        tenant_id=str(actor.tenant_id),
        config_id=str(config.id),
        case_id=str(case.id),
        scope=case.scope_json,
        order_reference=case.order_reference,
    )
    so = deepcopy(message.structured_output)
    so["accounting_review"] = p
    message.structured_output = so
    await db.flush()
    run = await accounting_recheck.queue(db, actor.tenant_id, message, actor.id, now=now - timedelta(seconds=1))
    await db.commit()
    # The config row is immutable evidence, so its ceiling is whatever the fixture set.
    assert run.max_api_calls == min(config.max_api_calls, accounting_recheck.recheck_call_ceiling(p))

    token = await state_service.claim_run(db, actor.tenant_id, run.id, now=now)
    spy = AsyncMock()
    monkeypatch.setattr(recheck, "reconcile", spy)
    interim = deepcopy(report)
    interim["order_reference"] = case.order_reference
    interim["targets"][0]["record_id"] = p["invoice_id"]  # the credit's parent, not the sales order

    await state_service.record_finding(
        db, actor.tenant_id, run.id, case.order_reference, interim, lease_token=token, now=now, final=False
    )

    finding = await db.scalar(select(TransactionFinding).where(TransactionFinding.run_id == run.id))
    assert finding.report_json["balance"]["status"] == "not_verified"
    assert finding.report_json["balance"]["reason"] == "accounting_recheck_identity_or_freshness_unverified"
    spy.assert_not_awaited()


def test_mcp_recheck_ceiling_is_derived_from_the_read_budget(mcp_credit):  # noqa: F811
    p, _, _ = mcp_credit
    mcp = accounting_recheck.recheck_call_ceiling(p)
    assert mcp == accounting_recheck.RECHECK_CALLS + recheck.READ_CALLS + accounting_recheck.MCP_RECHECK_HEADROOM
    assert mcp == 128  # the ceiling the previous literal allowed; not silently shrunk
    legacy = {**p, "execution_transport": None}
    assert accounting_recheck.recheck_call_ceiling(legacy) == accounting_recheck.RECHECK_CALLS


async def test_lease_lost_during_recheck_still_audits_the_exit_and_a_captured_defect(mcp_credit, monkeypatch):  # noqa: F811
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    run = _run(now)
    audit, commit, _, lease = _patch_state(monkeypatch, run)
    lease.side_effect = state_service.StateError("run_lease_lost")
    monkeypatch.setattr(credit_api_correction, "fresh", AsyncMock(side_effect=RuntimeError("session poisoned")))

    with pytest.raises(state_service.StateError) as raised:
        await recheck.reconcile(AsyncMock(), p["tenant_id"], run, p, report)

    assert isinstance(raised.value.__cause__, RuntimeError)  # the defect rides along, never dropped
    posted = _posted(audit)
    assert len(posted) == 1
    payload = posted[0].kwargs["payload"]
    assert payload["balance"]["status"] == "not_verified"
    assert payload["balance"]["reason"] == "credit_recheck_lease_lost"
    assert payload["error_type"] == "RuntimeError"
    assert payload["financial_writes"] == 0
    assert commit.await_count == 2  # reservation, then the audit that must survive the raise


async def test_lease_lost_after_a_clean_read_publishes_nothing_but_the_audit(mcp_credit, monkeypatch):  # noqa: F811
    now = datetime.now(timezone.utc)
    p, current, report = corrected(mcp_credit, now)
    run = _run(now)
    audit, commit, _, lease = _patch_state(monkeypatch, run)
    lease.side_effect = state_service.StateError("run_lease_lost")
    monkeypatch.setattr(credit_api_correction, "fresh", AsyncMock(return_value=current))

    with pytest.raises(state_service.StateError):
        await recheck.reconcile(AsyncMock(), p["tenant_id"], run, p, report)

    posted = _posted(audit)
    assert len(posted) == 1
    assert posted[0].kwargs["payload"]["balance"]["reason"] == "credit_recheck_lease_lost"
    assert "error_type" not in posted[0].kwargs["payload"]
    assert commit.await_count == 2


async def test_interim_write_with_a_mismatched_approval_binds_closed_instead_of_raising(mcp_credit, monkeypatch):  # noqa: F811
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    monkeypatch.setattr(
        accounting_recheck,
        "approval_for_run",
        AsyncMock(side_effect=state_service.StateError("accounting_recheck_approval_mismatch")),
    )
    spy = AsyncMock()
    monkeypatch.setattr(recheck, "reconcile", spy)
    run = SimpleNamespace(params_json={"verified_at": (now - timedelta(seconds=1)).isoformat()})

    bound = await accounting_recheck.bound_report(
        AsyncMock(), p["tenant_id"], run, report, now=now, subledger_recheck=False
    )

    assert bound["balance"]["status"] == "not_verified"
    assert bound["balance"]["reason"] == "accounting_recheck_approval_mismatch"
    assert bound["evidence_limits"] == {"code": "accounting_recheck_approval_mismatch"}
    spy.assert_not_awaited()


async def test_interim_finding_survives_an_approval_that_stopped_matching(
    db,
    approved_credit,  # noqa: F811
    mcp_credit,  # noqa: F811
    monkeypatch,
):
    """The run must terminate normally even when its approval is revoked mid-run; the finding binds closed."""
    actor, config, case, message, _, _ = approved_credit
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    p.update(
        tenant_id=str(actor.tenant_id),
        config_id=str(config.id),
        case_id=str(case.id),
        scope=case.scope_json,
        order_reference=case.order_reference,
    )
    so = deepcopy(message.structured_output)
    so["accounting_review"] = p
    message.structured_output = so
    await db.flush()
    run = await accounting_recheck.queue(db, actor.tenant_id, message, actor.id, now=now - timedelta(seconds=1))
    await db.commit()
    token = await state_service.claim_run(db, actor.tenant_id, run.id, now=now)
    message.structured_output = {**message.structured_output, "status": "rejected"}  # revoked after queueing
    await db.flush()
    spy = AsyncMock()
    monkeypatch.setattr(recheck, "reconcile", spy)
    interim = deepcopy(report)
    interim["order_reference"] = case.order_reference

    await state_service.record_finding(
        db, actor.tenant_id, run.id, case.order_reference, interim, lease_token=token, now=now, final=False
    )

    finding = await db.scalar(select(TransactionFinding).where(TransactionFinding.run_id == run.id))
    assert finding.report_json["balance"]["status"] == "not_verified"
    assert finding.report_json["balance"]["reason"] == "accounting_recheck_approval_mismatch"
    spy.assert_not_awaited()


def test_mcp_recheck_ceiling_applies_to_every_mcp_transported_kind(mcp_credit):  # noqa: F811
    p, _, _ = mcp_credit
    other = {**p, "kind": "sales_order_line_alignment"}
    assert accounting_recheck.recheck_call_ceiling(other) == 128
    assert accounting_recheck.needs_subledger_recheck(other) is False  # but only the credit re-reads the subledger


async def test_defect_rolls_back_the_session_before_the_ownership_recheck(mcp_credit, monkeypatch):  # noqa: F811
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    run = _run(now)
    _patch_state(monkeypatch, run)
    monkeypatch.setattr(credit_api_correction, "fresh", AsyncMock(side_effect=RuntimeError("aborted transaction")))
    db = AsyncMock()
    with pytest.raises(RuntimeError):
        await recheck.reconcile(db, p["tenant_id"], run, p, report)
    db.rollback.assert_awaited_once()


async def test_evidence_failure_does_not_roll_back_the_session(mcp_credit, monkeypatch):  # noqa: F811
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    run = _run(now)
    _patch_state(monkeypatch, run)
    monkeypatch.setattr(
        credit_api_correction, "fresh", AsyncMock(side_effect=SourceReadError("source_transport_failed"))
    )
    db = AsyncMock()
    await recheck.reconcile(db, p["tenant_id"], run, p, report)
    db.rollback.assert_not_awaited()
