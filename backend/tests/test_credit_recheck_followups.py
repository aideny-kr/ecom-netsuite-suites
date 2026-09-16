"""Follow-ups from the review of the credit-recheck fix (PR #262, 6fb2d316).

1. Every recheck exit writes the posting_observed audit and rechecks the lease. Failures
   of the evidence, including transport failures the previous except-tuple missed and
   the runner's own {"complete": False} fallback shapes, degrade to not_verified. A
   genuine defect is audited, committed, and then surfaced, never reclassified.
2. Interim findings of a recheck run stay bound to the approval; only the expensive
   subledger recheck waits for the final write.
3. The MCP recheck ceiling is derived from the read budget it exists to afford.
"""

from contextlib import asynccontextmanager
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


def _patch_state(monkeypatch, run, *, read_db=None, real_read_session=False):
    audit, commit, get_run, lease = AsyncMock(), AsyncMock(), AsyncMock(return_value=run), Mock()
    monkeypatch.setattr(recheck.state, "_audit", audit)
    monkeypatch.setattr(recheck.state, "_commit", commit)
    monkeypatch.setattr(recheck.state, "get_run", get_run)
    monkeypatch.setattr(recheck.state, "_lease", lease)
    if real_read_session:
        return audit, commit, get_run, lease

    @asynccontextmanager
    async def read_session(db):
        yield read_db if read_db is not None else db

    monkeypatch.setattr(recheck, "_read_session", read_session)
    monkeypatch.setattr(recheck, "set_tenant_context", AsyncMock())
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


@pytest.mark.parametrize("final", [False, True], ids=["interim", "final"])
@pytest.mark.parametrize(
    "raised, published",
    [
        ("accounting_recheck_approval_mismatch", "accounting_recheck_approval_mismatch"),
        ("not_found", "accounting_recheck_approval_unavailable"),  # a foreign lookup code is never published verbatim
    ],
)
async def test_a_write_with_a_mismatched_approval_binds_closed_instead_of_raising(
    mcp_credit,  # noqa: F811
    monkeypatch,
    final,
    raised,
    published,
):
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    monkeypatch.setattr(accounting_recheck, "approval_for_run", AsyncMock(side_effect=state_service.StateError(raised)))
    spy = AsyncMock()
    monkeypatch.setattr(recheck, "reconcile", spy)
    run = SimpleNamespace(params_json={"verified_at": (now - timedelta(seconds=1)).isoformat()})

    bound = await accounting_recheck.bound_report(
        AsyncMock(), p["tenant_id"], run, report, now=now, subledger_recheck=final
    )

    assert bound["balance"]["status"] == "not_verified"
    assert bound["balance"]["reason"] == published
    assert bound["evidence_limits"] == {"code": published}
    spy.assert_not_awaited()  # the final write never spends the subledger budget on a dead approval


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


@pytest.mark.parametrize(
    "raised, expected",
    [(RuntimeError("aborted transaction"), RuntimeError), (TimeoutError(), None)],
    ids=["defect", "timeout"],
)
async def test_a_failed_read_never_touches_the_callers_session(mcp_credit, monkeypatch, raised, expected):  # noqa: F811
    """The provider read runs on its own session; the caller's rows stay loaded and locked."""
    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    run = _run(now)
    read_db = AsyncMock(name="read_session")
    audit, _, get_run, _ = _patch_state(monkeypatch, run, read_db=read_db)
    fresh = AsyncMock(side_effect=raised)
    monkeypatch.setattr(credit_api_correction, "fresh", fresh)
    db = AsyncMock(name="caller_session")
    if expected is None:
        await recheck.reconcile(db, p["tenant_id"], run, p, report)
    else:
        with pytest.raises(expected):
            await recheck.reconcile(db, p["tenant_id"], run, p, report)
    assert fresh.await_args.args[0] is read_db
    db.rollback.assert_not_awaited()
    assert get_run.await_args.args[2] == run.id
    assert len(_posted(audit)) == 1


@pytest.mark.parametrize(
    "raised, expected, outcome",
    [
        (RuntimeError("aborted transaction"), RuntimeError, "credit_recheck_internal_error"),
        (TimeoutError(), None, "credit_recheck_evidence_unavailable"),
    ],
    ids=["defect", "timeout"],
)
async def test_failed_read_on_a_real_session_still_audits_the_exit(
    db,
    approved_credit,  # noqa: F811
    mcp_credit,  # noqa: F811
    monkeypatch,
    raised,
    expected,
    outcome,
):
    """reconcile() must leave the caller's real AsyncSession usable after a failed read.

    The unit tests drive reconcile() with an AsyncMock session and a SimpleNamespace
    run, so a read that poisoned the caller's session (and the rollback that then
    expired the caller's rows) were invisible to them. This runs the real thing: the
    read touches its session and fails; the exit must still be audited and committed,
    and the run row record_finding holds must still be readable without IO.
    """
    from app.models.audit import AuditEvent

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
    await state_service.claim_run(db, actor.tenant_id, run.id, now=now)
    run = await state_service.get_run(db, actor.tenant_id, run.id, lock=True)  # as record_finding holds it
    assert run.max_api_calls >= recheck.READ_CALLS, "fixture budget must afford the read"
    tenant_id, message_id = actor.tenant_id, str(message.id)
    seen = {}

    async def failing_read(read_db, tenant_id, proposal):
        seen["session"] = read_db
        await read_db.execute(select(TransactionFinding).where(TransactionFinding.run_id == run.id))
        raise raised

    monkeypatch.setattr(credit_api_correction, "fresh", failing_read)

    if expected is None:
        result = await recheck.reconcile(db, tenant_id, run, p, report)
        assert result["balance"]["status"] == "not_verified"
        assert result["balance"]["reason"] == outcome
    else:
        with pytest.raises(expected):
            await recheck.reconcile(db, tenant_id, run, p, report)

    assert seen["session"] is not db  # the read never ran on the caller's session
    # The caller's rows are still loaded: record_finding reads these synchronously next.
    assert run.status == "running" and run.deadline_at is not None
    # The exit was audited and committed regardless of what the read did to its session.
    audits = (
        await db.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.action == "transaction_ops.accounting_recheck.posting_observed",
                AuditEvent.payload["approval_message_id"].astext == message_id,
            )
        )
    ).all()
    assert len(audits) == 1
    assert audits[0].payload["balance"]["reason"] == outcome


async def test_a_statement_cancelled_by_the_timeout_costs_one_read_connection_and_nothing_else(
    mcp_credit,  # noqa: F811
    monkeypatch,
):
    """The production shape: the caller's session is bound to an engine, the read session
    is a second pooled connection. Cancelling a statement there invalidates that one
    connection; the caller keeps its connection, its transaction and its rows.

    The shared pytest fixture cannot host this test: on its single connection the same
    cancellation invalidates the caller too (PendingRollbackError), which is exactly why
    the read has its own session.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from tests.conftest import _test_connect_args, _test_db_url

    now = datetime.now(timezone.utc)
    p, _, report = corrected(mcp_credit, now)
    run = _run(now)
    run.deadline_at = now + timedelta(seconds=1.5)  # reconcile's timeout is min(90, remaining)
    audit, _, _, _ = _patch_state(monkeypatch, run, real_read_session=True)
    sessions = {}

    async def slow_read(read_db, tenant_id, proposal):
        sessions["read"] = read_db
        await read_db.execute(text("SELECT pg_sleep(30)"))
        raise AssertionError("the timeout must cancel the statement")

    monkeypatch.setattr(credit_api_correction, "fresh", slow_read)
    engine = create_async_engine(_test_db_url, connect_args=_test_connect_args, pool_size=2, max_overflow=1)
    try:
        async with AsyncSession(bind=engine, expire_on_commit=False) as db:
            await db.execute(text("SELECT 1"))  # the caller holds a connection and an open transaction
            result = await recheck.reconcile(db, p["tenant_id"], run, p, report)
            assert result["balance"] == {
                **report["balance"],
                "status": "not_verified",
                "reason": "credit_recheck_evidence_unavailable",
            }
            assert sessions["read"] is not db
            assert sessions["read"].bind is engine  # from the caller's own engine, never the global pool
            assert len(_posted(audit)) == 1
            # The caller's connection survived the cancellation and is still the only one checked out.
            assert (await db.execute(text("SELECT 1"))).scalar() == 1
            assert db.in_transaction()
            assert engine.pool.checkedout() == 1
        assert engine.pool.checkedout() == 0  # nothing leaked
    finally:
        await engine.dispose()
