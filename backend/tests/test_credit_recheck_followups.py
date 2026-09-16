"""Follow-ups from the review of the credit-recheck fix (PR #262, 6fb2d316).

1. Every recheck exit writes the posting_observed audit and rechecks the lease, including
   transport failures the previous except-tuple missed and programming errors, which are
   audited and then surfaced instead of being reclassified as missing evidence.
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
    audit, get_run, lease = AsyncMock(), AsyncMock(return_value=run), Mock()
    monkeypatch.setattr(recheck.state, "_audit", audit)
    monkeypatch.setattr(recheck.state, "_commit", AsyncMock())
    monkeypatch.setattr(recheck.state, "get_run", get_run)
    monkeypatch.setattr(recheck.state, "_lease", lease)
    return audit, get_run, lease


def _posted(audit):
    return [c for c in audit.call_args_list if c.args[2] == "accounting_recheck.posting_observed"]


@pytest.mark.parametrize(
    "exc",
    [SourceReadError("source_transport_failed"), httpx.ConnectError("connection reset"), TimeoutError()],
    ids=["source_reader", "httpx", "timeout"],
)
async def test_transport_failure_during_recheck_is_not_verified_with_audit_and_lease_recheck(
    mcp_credit,  # noqa: F811
    monkeypatch,
    exc,
):
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    run = _run(now)
    audit, get_run, lease = _patch_state(monkeypatch, run)
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
    assert run.api_calls_used == recheck.READ_CALLS  # a failed read still consumed its reservation


async def test_programming_error_during_recheck_is_audited_then_surfaced(mcp_credit, monkeypatch):  # noqa: F811
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    run = _run(now)
    audit, get_run, lease = _patch_state(monkeypatch, run)
    monkeypatch.setattr(credit_api_correction, "fresh", AsyncMock(side_effect=KeyError("refund_evidence")))

    with pytest.raises(KeyError):
        await recheck.reconcile(AsyncMock(), p["tenant_id"], run, p, report)

    posted = _posted(audit)
    assert len(posted) == 1
    payload = posted[0].kwargs["payload"]
    assert payload["financial_writes"] == 0
    assert payload["balance"]["status"] == "not_verified"
    assert payload["balance"]["reason"] == "credit_recheck_internal_error"
    assert payload["error_type"] == "KeyError"
    get_run.assert_awaited_once()
    lease.assert_called_once()


async def test_report_without_refund_evidence_is_a_verification_failure_not_a_crash(mcp_credit):  # noqa: F811
    now = datetime.now(timezone.utc)
    p, current, report = corrected(mcp_credit, now)
    del report["refund_evidence"]
    with pytest.raises(ValueError):
        recheck.project(p, report, current, verified_at=now - timedelta(seconds=1), now=now)
    p, current, report = corrected(mcp_credit, now)
    del report["balance"]["amounts"]
    with pytest.raises(ValueError):
        recheck.project(p, report, current, verified_at=now - timedelta(seconds=1), now=now)


async def test_interim_bound_report_annotates_scope_without_the_expensive_recheck(mcp_credit, monkeypatch):  # noqa: F811
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    monkeypatch.setattr(accounting_recheck, "approval_for_run", AsyncMock(return_value=(SimpleNamespace(), p)))
    spy = AsyncMock(return_value={"reconciled": True})
    monkeypatch.setattr(recheck, "reconcile", spy)
    run = SimpleNamespace(params_json={"verified_at": (now - timedelta(seconds=1)).isoformat()})

    interim = await accounting_recheck.bound_report(AsyncMock(), p["tenant_id"], run, report, now=now, reconcile=False)
    assert interim == report
    spy.assert_not_awaited()

    final = await accounting_recheck.bound_report(AsyncMock(), p["tenant_id"], run, report, now=now, reconcile=True)
    assert final == {"reconciled": True}
    spy.assert_awaited_once()

    out_of_scope = deepcopy(report)
    out_of_scope["targets"][0]["record_id"] = p["invoice_id"]
    bound = await accounting_recheck.bound_report(
        AsyncMock(), p["tenant_id"], run, out_of_scope, now=now, reconcile=False
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
    assert accounting_recheck.recheck_call_ceiling(p) == accounting_recheck.RECHECK_CALLS + recheck.READ_CALLS
    legacy = {**p, "execution_transport": None}
    assert accounting_recheck.recheck_call_ceiling(legacy) == accounting_recheck.RECHECK_CALLS
