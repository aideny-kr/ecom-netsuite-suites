from datetime import datetime, timezone

from app.schemas.transaction_runs import ProposalDecision, RunCreate
from app.services.transaction_ops import planner
from app.services.transaction_ops import state_service as state
from tests import test_transaction_ops_executor as fixtures

execution_case = fixtures.execution_case


async def replan(db, case, key):
    tenant = case.actor.tenant_id
    run = await state.create_run(
        db,
        tenant,
        case.case.config.id,
        RunCreate(evaluation_key=key, order_references=[case.proposal.order_reference]),
        actor=case.actor,
    )
    token = await state.claim_run(db, tenant, run.id)
    request = planner.plan_proposal(
        case.case.report, case.before, case.case.config, now=datetime.now(timezone.utc), guard=case.case.guard
    )
    proposal = await state.propose(db, tenant, run.id, request, lease_token=token)
    await state.finish_run(db, tenant, run.id, "done", lease_token=token)
    return proposal


async def test_known_no_write_failure_allows_one_fresh_human_approval_for_identical_work(db, execution_case):
    case = execution_case
    case.case.read_source.side_effect = RuntimeError("temporary read unavailable")
    assert (await fixtures.execute(db, case))["status"] == "failed"
    previous = await fixtures.operation(db, case)
    retry = await replan(db, case, "retry-1")
    assert retry.id != case.proposal.id and retry.work_key != case.proposal.work_key
    assert retry.status == "pending" and retry.decided_by is None
    assert retry.before_json == case.proposal.before_json and retry.after_json == case.proposal.after_json
    assert retry.evidence_fingerprint == case.proposal.evidence_fingerprint
    assert retry.evidence_json["retry"] == {"previous_operation_id": str(previous.id), "attempt": 2}
    assert (await replan(db, case, "retry-redelivery")).id == retry.id
    # A pending retry is never execution authority.
    assert (await fixtures.mod.execute_proposal(db, case.actor.tenant_id, retry.id))["status"] == "pending"
    case.case.dispatch.assert_not_awaited()
    await state.decide_proposal(
        db,
        case.actor.tenant_id,
        retry.id,
        ProposalDecision(decision="approve", evidence_fingerprint=retry.evidence_fingerprint),
        actor=case.actor,
    )
    assert (await fixtures.mod.execute_proposal(db, case.actor.tenant_id, retry.id))["status"] == "failed"
    assert (await replan(db, case, "retry-3-blocked")).id == retry.id


async def test_rejection_of_the_retry_is_sticky_across_subsequent_investigations(db, execution_case):
    case = execution_case
    case.case.read_source.side_effect = RuntimeError("temporary read unavailable")
    await fixtures.execute(db, case)
    retry = await replan(db, case, "retry-1")
    await state.decide_proposal(
        db,
        case.actor.tenant_id,
        retry.id,
        ProposalDecision(decision="reject", evidence_fingerprint=retry.evidence_fingerprint),
        actor=case.actor,
    )
    assert (await replan(db, case, "retry-after-reject")).id == retry.id
    case.case.dispatch.assert_not_awaited()


async def test_unknown_outcome_cannot_be_replanned_into_a_new_attempt(db, execution_case):
    case = execution_case
    case.case.read_target.side_effect = [case.before, case.before]
    case.case.read_guard.side_effect = [case.case.guard, case.case.guard]
    assert (await fixtures.execute(db, case))["status"] == "unknown"
    assert (await replan(db, case, "unknown-retry-blocked")).id == case.proposal.id
    case.case.dispatch.assert_awaited_once()
