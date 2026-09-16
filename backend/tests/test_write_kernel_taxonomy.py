"""G3.1a: the operation ledger's outcome taxonomy.

The ledger is the write kernel (docs/superpowers/specs/2026-09-15-write-kernel-design.md,
sections 2 and 3). Its statuses become

    executing | rejected_before_effect | committed_unverified | unknown | verified | needs_review

and the repair rule is a function of that status: a retry is allowed only from
``rejected_before_effect`` (as a lineage row), ``unknown`` may only be reconciled by reads,
``committed_unverified`` may only be verified by reads, ``needs_review`` waits for a person.
The legacy ``failed`` value stays accepted by the CHECK for one release so branches still
writing it keep working against a shared database; nothing here writes it.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.audit import AuditEvent
from app.models.transaction_ops import TransactionOperation
from app.services.transaction_ops import recovery
from app.services.transaction_ops import state_service as state
from tests import test_transaction_ops_dispatch as dispatch_fixtures
from tests import test_transaction_ops_executor as execution_fixtures
from tests.test_transaction_ops_dispatch import reserve
from tests.test_transaction_ops_executor import execute, operation
from tests.test_transaction_ops_state_db import new_proposal

execution_case = execution_fixtures.execution_case
ready = dispatch_fixtures.ready


async def _claimed_row(db, claim):
    return await db.scalar(select(TransactionOperation).where(TransactionOperation.id == claim.operation_id))


# ---------------------------------------------------------------- the database's own vocabulary


@pytest.mark.parametrize(
    "status", ["executing", "rejected_before_effect", "committed_unverified", "unknown", "verified", "needs_review"]
)
async def test_the_ledger_accepts_each_kernel_status(db, ready, status):
    actor, _, _, claim = ready
    if status == "committed_unverified":
        assert await reserve(db, actor.tenant_id, claim)  # a receipt is only ever recorded behind the permit
    await db.execute(
        text("UPDATE transaction_ops_operations SET status = :status WHERE id = :id"),
        {"status": status, "id": claim.operation_id},
    )
    assert (await _claimed_row(db, claim)).status == status


async def test_the_ledger_refuses_a_status_outside_the_taxonomy(db, ready):
    actor, _, _, claim = ready
    with pytest.raises(IntegrityError):
        await db.execute(
            text("UPDATE transaction_ops_operations SET status = 'exploded' WHERE id = :id"),
            {"id": claim.operation_id},
        )
    await db.rollback()


async def test_the_claim_records_its_approval_source_surface_and_lineage(db, ready):
    actor, _, _, claim = ready
    row = await _claimed_row(db, claim)
    assert row.approval_kind == "transaction_proposal"
    assert row.approval_id == claim.proposal_id
    assert row.surface == "scheduled"
    assert row.provider == "netsuite"
    assert row.adapter == "guard_restlet"
    assert row.base_work_key == row.work_key
    assert row.retry_of_operation_id is None


async def test_a_legacy_writer_insert_is_completed_by_the_database(db, ready):
    """A previous release (or a branch without this revision) inserts a proposal-approved row
    without the new columns; the defaults trigger fills them the way the service would."""
    actor, _, _, claim = ready
    await db.execute(text("DELETE FROM transaction_ops_operations WHERE id = :id"), {"id": claim.operation_id})
    legacy_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO transaction_ops_operations (id, tenant_id, proposal_id, work_key, entity_key, status, "
            "attempted_at, deadline_at, max_api_calls, api_calls_used, result_json, created_at, updated_at) "
            "VALUES (:id, :tenant_id, :proposal_id, :work_key, :entity_key, 'executing', now(), "
            "now() + interval '5 minutes', 96, 0, '{}'::jsonb, now(), now())"
        ),
        {
            "id": legacy_id,
            "tenant_id": actor.tenant_id,
            "proposal_id": claim.proposal_id,
            "work_key": claim.work_key,
            "entity_key": "e" * 64,
        },
    )
    row = await db.scalar(select(TransactionOperation).where(TransactionOperation.id == legacy_id))
    assert row.approval_kind == "transaction_proposal" and row.approval_id == claim.proposal_id
    assert row.base_work_key == claim.work_key and row.surface == "scheduled"


async def test_terminal_rows_are_frozen_by_the_database(db, ready):
    """The guard trigger, not application code, is what makes a terminal outcome final."""
    actor, _, _, claim = ready
    await state.complete_operation(
        db, actor.tenant_id, claim.operation_id, outcome="rejected_before_effect", result_json={"code": "x"}
    )
    with pytest.raises(Exception) as exc:
        await db.execute(
            text("UPDATE transaction_ops_operations SET status = 'executing' WHERE id = :id"),
            {"id": claim.operation_id},
        )
    assert "immutable operation attempt" in str(exc.value)
    await db.rollback()


def _confirmation_row(actor, *, status="executing", work="w", entity="e", result_json=None):
    """A ledger row claimed by a chat confirmation: no proposal, its approval on the row."""
    now = datetime.now(timezone.utc)
    return TransactionOperation(
        tenant_id=actor.tenant_id,
        proposal_id=None,
        approval_kind="chat_confirmation",
        approval_id=uuid.uuid4(),
        surface="chat",
        provider="netsuite_mcp",
        adapter="credit_api",
        work_key=work * 64,
        entity_key=entity * 64,
        base_work_key=work * 64,
        attempted_at=now,
        deadline_at=now + timedelta(minutes=5),
        max_api_calls=96,
        api_calls_used=0,
        status=status,
        result_json={"approved_by": str(actor.id), "evidence_digest": "d" * 64, **(result_json or {})},
    )


@pytest.mark.parametrize("outcome", ["rejected_before_effect", "needs_review", "verified"])
async def test_a_confirmation_row_completes_without_a_proposal(db, ready, outcome):
    """A row claimed by a chat confirmation has no proposal; completing it records the
    approval it does have, never looks for the proposal it does not, and a verified
    outcome queues no case settlement (the card's own surface owns what follows)."""
    from app.models.transaction_ops import TransactionRun

    actor, _, _, _ = ready
    row = _confirmation_row(actor)
    db.add(row)
    await db.flush()
    evidence = {"code": "x"}
    if outcome == "verified":
        # verified needs a consumed permit and a readback proof
        row.api_calls_used += 1
        row.result_json = {**row.result_json, "dispatch_reserved": True, "provider": "netsuite_mcp"}
        await db.flush()
        evidence["verification"] = {"source_unchanged": True}
    done = await state.complete_operation(db, actor.tenant_id, row.id, outcome=outcome, result_json=evidence)
    assert done.status == outcome and done.completed_at is not None
    settlement = await db.scalar(
        select(TransactionRun.id).where(TransactionRun.params_json["operation_id"].astext == str(row.id))
    )
    assert settlement is None
    audit = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_id == str(row.id), AuditEvent.action == "transaction_ops.operation.complete"
        )
    )
    assert audit.payload["approval_kind"] == "chat_confirmation"
    assert audit.payload["approval_id"] == str(row.approval_id)
    assert audit.payload["approved_by"] == str(actor.id)
    assert "run_id" not in audit.payload


def _migration():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "109_write_kernel_operations.py"
    spec = importlib.util.spec_from_file_location("migration_109", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _run_step(connection, step):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with Operations.context(MigrationContext.configure(connection)):
        step()


async def test_the_downgrade_refuses_to_erase_confirmation_rows(db, ready):
    """A downgrade cannot un-happen a send: rows a legacy reader cannot address are kept
    by refusing the downgrade, never deleted."""
    actor, _, _, _ = ready
    db.add(_confirmation_row(actor, status="needs_review"))
    await db.commit()
    connection = await db.connection()
    with pytest.raises(RuntimeError, match="proposal"):
        await connection.run_sync(_run_step, _migration().downgrade)


@pytest.mark.parametrize("permit", [False, True])
async def test_the_downgrade_folds_needs_review_to_unknown_whether_or_not_a_permit_was_consumed(db, ready, permit):
    """needs_review has no legacy value and may hide an effect (an adapter-reported save the
    ledger could not tie to a permit is the permit-less case), so it becomes unknown, which
    blocks a resend; only rejected_before_effect becomes failed."""
    actor, _, _, claim = ready
    if permit:
        assert await reserve(db, actor.tenant_id, claim)
    await state.complete_operation(
        db, actor.tenant_id, claim.operation_id, outcome="needs_review", result_json={"code": "x"}
    )
    await db.commit()
    connection = await db.connection()
    await connection.run_sync(_run_step, _migration().downgrade)
    assert (
        await db.scalar(
            text("SELECT status FROM transaction_ops_operations WHERE id = :id"), {"id": claim.operation_id}
        )
    ) == "unknown"
    await connection.run_sync(_run_step, _migration().upgrade)


async def test_the_downgrade_refuses_two_open_attempts_on_one_document(db, ready):
    actor, _, proposal, claim = ready
    executing = (
        await db.execute(select(TransactionOperation).where(TransactionOperation.id == claim.operation_id))
    ).scalar_one()
    run = await state.get_run(db, actor.tenant_id, proposal.run_id)
    other = await new_proposal(db, actor, run, currency="EUR")  # a second piece of work on the same order
    sent = _confirmation_row(actor, status="needs_review", work="s")
    sent.proposal_id = sent.approval_id = other.id
    sent.approval_kind, sent.surface = "transaction_proposal", "scheduled"
    sent.work_key = sent.base_work_key = other.work_key
    sent.entity_key = executing.entity_key
    db.add(sent)
    await db.commit()
    connection = await db.connection()
    with pytest.raises(RuntimeError, match="document"):
        await connection.run_sync(_run_step, _migration().downgrade)


async def test_the_migration_renames_legacy_failed_rows_under_the_legacy_check(db, ready):
    """Run 109's downgrade and upgrade in-process, inside the test transaction, with a
    legacy ``failed`` row present: the rename must happen while the CHECK that allows the
    new value is in force. Gate round three: the UPDATE ran before the CHECK swap."""
    migration = _migration()
    actor, _, _, claim = ready
    await db.commit()  # the claim's savepoint; DDL below runs on the same connection
    connection = await db.connection()
    await connection.run_sync(_run_step, migration.downgrade)
    await db.execute(
        text("UPDATE transaction_ops_operations SET status = 'failed' WHERE id = :id"), {"id": claim.operation_id}
    )
    await connection.run_sync(_run_step, migration.upgrade)
    status = await db.scalar(
        text("SELECT status FROM transaction_ops_operations WHERE id = :id"), {"id": claim.operation_id}
    )
    assert status == "rejected_before_effect"


# ---------------------------------------------------------------- complete_operation


async def test_rejected_before_effect_is_terminal_with_an_error_reason(db, ready):
    actor, _, _, claim = ready
    row = await state.complete_operation(
        db,
        actor.tenant_id,
        claim.operation_id,
        outcome="rejected_before_effect",
        result_json={"code": "precondition_changed:approved_evidence_changed"},
    )
    assert row.status == "rejected_before_effect"
    assert row.result_json["termination_reason"] == "error"
    with pytest.raises(state.StateError, match="operation_terminal"):
        await state.complete_operation(
            db, actor.tenant_id, claim.operation_id, outcome="verified", result_json={"code": "later"}
        )


async def test_committed_unverified_waits_for_verification_and_can_still_become_verified(db, ready):
    actor, _, _, claim = ready
    assert await reserve(db, actor.tenant_id, claim)
    row = await state.complete_operation(
        db,
        actor.tenant_id,
        claim.operation_id,
        outcome="committed_unverified",
        result_json={"code": "verification_unproven", "receipt": {"record_id": "63"}},
    )
    assert row.status == "committed_unverified"
    assert row.result_json["termination_reason"] == "stall"
    row = await state.complete_operation(
        db,
        actor.tenant_id,
        claim.operation_id,
        outcome="verified",
        result_json={"code": "independently_verified", "verification": {"source_unchanged": True}},
    )
    assert row.status == "verified" and row.result_json["termination_reason"] == "done"


async def test_needs_review_is_terminal_for_automation_and_says_so(db, ready):
    actor, _, _, claim = ready
    assert await reserve(db, actor.tenant_id, claim)
    row = await state.complete_operation(
        db,
        actor.tenant_id,
        claim.operation_id,
        outcome="needs_review",
        result_json={"code": "readback_contradicts_approval"},
    )
    assert row.status == "needs_review"
    assert row.result_json["termination_reason"] == "blocked"
    with pytest.raises(state.StateError, match="operation_terminal"):
        await state.complete_operation(
            db, actor.tenant_id, claim.operation_id, outcome="verified", result_json={"code": "later"}
        )


async def test_the_legacy_failed_outcome_is_no_longer_accepted_by_the_service(db, ready):
    actor, _, _, claim = ready
    with pytest.raises(ValueError):
        await state.complete_operation(db, actor.tenant_id, claim.operation_id, outcome="failed", result_json={})


async def test_unknown_still_needs_reconciliation_evidence_to_move(db, ready):
    actor, _, _, claim = ready
    assert await reserve(db, actor.tenant_id, claim)
    await state.complete_operation(
        db, actor.tenant_id, claim.operation_id, outcome="unknown", result_json={"code": "provider_timeout"}
    )
    with pytest.raises(state.StateError, match="reconciliation_evidence_required"):
        await state.complete_operation(
            db, actor.tenant_id, claim.operation_id, outcome="rejected_before_effect", result_json={"code": "absent"}
        )
    row = await state.complete_operation(
        db,
        actor.tenant_id,
        claim.operation_id,
        outcome="rejected_before_effect",
        result_json={"code": "reconciled_absent", "reconciled": True},
    )
    assert row.status == "rejected_before_effect"


# ---------------------------------------------------------------- budget and expiry fencing


async def test_budget_exhaustion_before_a_permit_is_rejected_before_effect(db, ready):
    actor, _, _, claim = ready
    assert await state.reserve_operation_budget(db, actor.tenant_id, claim.operation_id, api_calls=96)
    assert await state.reserve_operation_budget(db, actor.tenant_id, claim.operation_id, api_calls=1) is None
    row = await _claimed_row(db, claim)
    assert row.status == "rejected_before_effect"
    assert row.result_json["termination_reason"] == "budget"


@pytest.mark.parametrize("sent", [False, True])
async def test_expiry_recovery_is_rejected_before_effect_only_when_no_permit_was_consumed(db, ready, sent):
    actor, _, _, claim = ready
    if sent:
        assert await reserve(db, actor.tenant_id, claim)
    row = await _claimed_row(db, claim)
    recovered = await state.recover_expired_operation(db, actor.tenant_id, row.id, now=row.deadline_at)
    assert recovered.status == ("unknown" if sent else "rejected_before_effect")


# ---------------------------------------------------------------- the executor as a kernel client


async def test_changed_evidence_ends_rejected_before_effect_with_nothing_sent(db, execution_case):
    # The approved evidence drifted before the send: the fresh plan's fingerprint no longer
    # matches the approved one (the executor's source_amount case).
    from copy import deepcopy

    case = execution_case.case
    case.source = deepcopy(case.source)
    case.source["orders"][0]["total"] = "101"
    case.read_source.return_value = case.source
    result = await execute(db, execution_case)
    assert result["status"] == "rejected_before_effect" and result["termination_reason"] == "error"
    row = await operation(db, execution_case)
    # Drift is caught either by the fresh plan's fingerprint or by the planner refusing the
    # changed evidence outright; both end here, before any permit exists.
    assert row.result_json["code"] in {"approved_evidence_changed", "evidence_revalidation_failed"}
    assert row.result_json.get("dispatch_reserved") is not True
    execution_case.case.dispatch.assert_not_awaited()


async def test_a_provider_rejection_with_no_save_is_rejected_before_effect(db, execution_case):
    async def rejected(db_, tenant, claimed):
        assert await state.reserve_operation_dispatch(
            db_, tenant, claimed, provider="netsuite", payload_fingerprint="d" * 64
        )
        return {"status": "failed", "code": "guard_rejected", "verified": False}

    execution_case.case.dispatch.side_effect = rejected
    result = await execute(db, execution_case)
    assert result["status"] == "rejected_before_effect"
    row = await operation(db, execution_case)
    assert row.result_json["code"] == "provider_rejected_without_save"
    assert row.result_json["dispatch_reserved"] is True  # the permit was spent; the retry is a lineage row


async def test_an_accepted_receipt_is_recorded_as_committed_unverified_before_verification(db, execution_case):
    """Between the send and the readback the row says what is true: saved, not yet proven."""
    seen = []
    values = [execution_case.before, execution_case.after]

    async def read_target(*args, **kwargs):
        row = await operation(db, execution_case)
        seen.append((row.status, (row.result_json or {}).get("receipt")))
        return values.pop(0)

    execution_case.case.read_target.side_effect = read_target
    result = await execute(db, execution_case)
    assert result["status"] == "verified"
    # First target read: before the send (executing, no receipt). Second: after the send.
    assert seen[0][0] == "executing" and seen[0][1] is None
    assert seen[1][0] == "committed_unverified" and seen[1][1] == {
        "record_id": "63",
        "status": "accepted",
        "verified": False,
    }


async def test_an_accepted_receipt_without_proof_stays_committed_unverified(db, execution_case):
    execution_case.after["orders"][0]["header"]["total"] = "101"  # readback disagrees with the approved state
    result = await execute(db, execution_case)
    assert result["status"] == "committed_unverified" and result["termination_reason"] == "stall"
    row = await operation(db, execution_case)
    assert row.result_json["code"] == "verification_unproven"


async def test_recovery_verifies_a_committed_unverified_operation_by_reads(db, execution_case, monkeypatch):
    execution_case.after["orders"][0]["header"]["total"] = "101"
    assert (await execute(db, execution_case))["status"] == "committed_unverified"
    row = await operation(db, execution_case)
    monkeypatch.setattr(recovery, "read_framework_order", execution_case.case.read_source)
    corrected = dict(execution_case.after)
    corrected["orders"][0]["header"]["total"] = "100"
    monkeypatch.setattr(recovery, "read_netsuite_order", AsyncMock(return_value=corrected))
    monkeypatch.setattr(recovery, "read_guard_snapshot", AsyncMock(return_value=execution_case.after_guard))
    run = await state.create_operation_recovery(db, execution_case.actor.tenant_id, row.id)
    result = await recovery.reconcile_operation_run(db, execution_case.actor.tenant_id, run.id)
    assert result["status"] == "verified"


async def test_the_retry_of_a_rejected_operation_keeps_the_lineage(db, execution_case):
    async def rejected(db_, tenant, claimed):
        assert await state.reserve_operation_dispatch(
            db_, tenant, claimed, provider="netsuite", payload_fingerprint="d" * 64
        )
        return {"status": "failed", "code": "guard_rejected", "verified": False}

    execution_case.case.dispatch.side_effect = rejected
    assert (await execute(db, execution_case))["status"] == "rejected_before_effect"
    first = await operation(db, execution_case)
    # A second approval of the same work: the state service issues a lineage key and the
    # new operation points at the attempt it retries and shares its base work key.
    from app.schemas.transaction_runs import ProposalDecision
    from app.services.transaction_ops import executor as mod
    from tests.test_transaction_ops_known_failure_retry import replan

    retry = await replan(db, execution_case, "lineage-retry")
    assert retry.evidence_json["retry"]["previous_operation_id"] == str(first.id)
    await state.decide_proposal(
        db,
        execution_case.actor.tenant_id,
        retry.id,
        ProposalDecision(decision="approve", evidence_fingerprint=retry.evidence_fingerprint),
        actor=execution_case.actor,
    )
    await mod.execute_proposal(db, execution_case.actor.tenant_id, retry.id)
    second = await db.scalar(select(TransactionOperation).where(TransactionOperation.proposal_id == retry.id))
    assert second is not None
    assert second.retry_of_operation_id == first.id
    assert second.base_work_key == first.base_work_key == first.work_key
    assert second.work_key != first.work_key
    assert uuid.UUID(str(second.approval_id)) == retry.id
    assert second.attempted_at - first.attempted_at >= timedelta(0)


async def test_a_receipt_can_never_be_called_before_effect_or_unknown_again(db, ready):
    actor, _, _, claim = ready
    assert await reserve(db, actor.tenant_id, claim)
    await state.complete_operation(
        db, actor.tenant_id, claim.operation_id, outcome="committed_unverified", result_json={"code": "x"}
    )
    for outcome in ("rejected_before_effect", "unknown"):
        with pytest.raises(state.StateError, match="receipt_recorded"):
            await state.complete_operation(
                db, actor.tenant_id, claim.operation_id, outcome=outcome, result_json={"code": "y", "reconciled": True}
            )


async def test_a_receipt_becomes_verified_only_with_a_readback_proof(db, ready):
    actor, _, _, claim = ready
    assert await reserve(db, actor.tenant_id, claim)
    await state.complete_operation(
        db, actor.tenant_id, claim.operation_id, outcome="committed_unverified", result_json={"code": "x"}
    )
    with pytest.raises(state.StateError, match="verification_evidence_required"):
        await state.complete_operation(
            db, actor.tenant_id, claim.operation_id, outcome="verified", result_json={"code": "no proof"}
        )
    row = await state.complete_operation(
        db,
        actor.tenant_id,
        claim.operation_id,
        outcome="verified",
        result_json={"code": "independently_verified", "verification": {"source_unchanged": True}},
    )
    assert row.status == "verified"


@pytest.mark.parametrize("status", ["rejected_before_effect", "unknown", "failed"])
async def test_the_database_refuses_to_downgrade_a_receipt(db, ready, status):
    """The receipt invariant is the database's, not only complete_operation's: no writer,
    however it reaches the table, can call a receipted attempt before-effect or unknown."""
    actor, _, _, claim = ready
    assert await reserve(db, actor.tenant_id, claim)
    await state.complete_operation(
        db, actor.tenant_id, claim.operation_id, outcome="committed_unverified", result_json={"code": "x"}
    )
    with pytest.raises(Exception) as exc:
        await db.execute(
            text("UPDATE transaction_ops_operations SET status = :status WHERE id = :id"),
            {"id": claim.operation_id, "status": status},
        )
    assert "immutable operation receipt" in str(exc.value)
    await db.rollback()


async def test_the_database_refuses_a_receipt_on_a_row_that_never_consumed_a_permit(db, ready):
    actor, _, _, claim = ready
    with pytest.raises(Exception) as exc:
        await db.execute(
            text("UPDATE transaction_ops_operations SET status = 'committed_unverified' WHERE id = :id"),
            {"id": claim.operation_id},
        )
    assert "receipt requires a permit" in str(exc.value)
    await db.rollback()


async def test_the_database_refuses_to_verify_a_receipt_without_a_readback_proof(db, ready):
    actor, _, _, claim = ready
    assert await reserve(db, actor.tenant_id, claim)
    await state.complete_operation(
        db, actor.tenant_id, claim.operation_id, outcome="committed_unverified", result_json={"code": "x"}
    )
    with pytest.raises(Exception) as exc:
        await db.execute(
            text("UPDATE transaction_ops_operations SET status = 'verified' WHERE id = :id"),
            {"id": claim.operation_id},
        )
    assert "immutable operation receipt" in str(exc.value)
    await db.rollback()


async def test_a_committed_unverified_attempt_blocks_a_new_claim_on_the_same_order(db, ready):
    """The in-flight guard at claim time counts a receipted-but-unproven attempt as in flight,
    like the partial unique index, settlement and the scheduler already do."""
    from app.schemas.transaction_runs import ProposalDecision

    actor, _, proposal, claim = ready
    assert await reserve(db, actor.tenant_id, claim)
    await state.complete_operation(
        db, actor.tenant_id, claim.operation_id, outcome="committed_unverified", result_json={"code": "x"}
    )
    run = await state.get_run(db, actor.tenant_id, proposal.run_id)
    other = await new_proposal(db, actor, run, currency="EUR")  # a different piece of work, the same order
    assert other.id != proposal.id and other.work_key != proposal.work_key
    await state.decide_proposal(
        db,
        actor.tenant_id,
        other.id,
        ProposalDecision(decision="approve", evidence_fingerprint=other.evidence_fingerprint),
        actor=actor,
    )
    with pytest.raises(state.StateError, match="operation_already_attempted"):
        await state.claim_approved_operation(
            db, actor.tenant_id, other.id, expected_evidence_fingerprint=other.evidence_fingerprint
        )
