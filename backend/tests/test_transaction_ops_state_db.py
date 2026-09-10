"""Real PostgreSQL transaction/tenant/approval regression coverage."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.models.celigo import CeligoFlow, CeligoFlowStep, CeligoIntegration
from app.models.transaction_ops import TransactionOperation
from app.schemas.transaction_runs import ConfigControl, ProgressUpdate, ProposalDecision, RunCreate
from app.services.transaction_ops import state_service as state
from tests.test_transaction_ops_state import config_input, proposal_input


async def seed_config(db, tenant_id, actor, **config_changes):
    celigo_id, netsuite_id = uuid4(), uuid4()
    for identifier, provider in ((celigo_id, "celigo"), (netsuite_id, "netsuite")):
        await db.execute(
            text(
                "INSERT INTO connections (id,tenant_id,provider,label,status,encrypted_credentials,encryption_key_version) "
                "VALUES (:id,:tenant,:provider,'Transaction tests','active','not-read',1)"
            ),
            {"id": identifier, "tenant": tenant_id, "provider": provider},
        )
    integration = CeligoIntegration(
        tenant_id=tenant_id, celigo_connection_id=celigo_id, celigo_id=uuid4().hex[:24], name="Source", raw_json={}
    )
    db.add(integration)
    await db.flush()
    flow = CeligoFlow(
        tenant_id=tenant_id,
        celigo_connection_id=celigo_id,
        integration_id=integration.id,
        celigo_id=uuid4().hex[:24],
        name="Source",
        raw_json={},
    )
    db.add(flow)
    await db.flush()
    step = CeligoFlowStep(
        tenant_id=tenant_id,
        celigo_connection_id=celigo_id,
        flow_id=flow.id,
        celigo_id=uuid4().hex[:24],
        role="generator",
        connection_celigo_id=uuid4().hex[:24],
        raw_json={},
    )
    db.add(step)
    await db.flush()
    return await state.create_config(
        db,
        tenant_id,
        config_input(source_step_id=step.id, netsuite_connection_id=netsuite_id, **config_changes),
        actor=actor,
    )


@pytest.fixture
async def setup_state(db, admin_user):
    actor, _ = admin_user
    config = await seed_config(db, actor.tenant_id, actor)
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="manual-1", order_references=["R123456789"]),
        actor=actor,
    )
    return actor, config, run


async def new_proposal(db, actor, run, **changes):
    token = run.lease_token or await state.claim_run(db, actor.tenant_id, run.id)
    return await state.propose(
        db,
        actor.tenant_id,
        run.id,
        proposal_input(observed_at=datetime.now(timezone.utc), **changes),
        lease_token=token,
    )


async def test_run_and_proposal_dedupe_retries_by_business_work(db, setup_state):
    actor, config, run = setup_state
    retry = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="manual-1", order_references=["R123456789"]),
        actor=actor,
    )
    assert retry.id == run.id
    first = await new_proposal(db, actor, run)
    again = await new_proposal(db, actor, run)
    assert first.id == again.id


async def test_invalid_mapping_cannot_create_an_operational_config(db, admin_user):
    actor, _ = admin_user
    with pytest.raises(state.StateError, match="invalid_mapping"):
        await seed_config(db, actor.tenant_id, actor, mapping_json={"reference_field": "tranid OR 1=1"})


async def test_budget_reservation_is_persistent_and_never_overspends(db, setup_state):
    actor, _, run = setup_state
    token = await state.claim_run(db, actor.tenant_id, run.id)
    assert token
    assert await state.claim_run(db, actor.tenant_id, run.id) is None
    assert await state.reserve_budget(
        db, actor.tenant_id, run.id, lease_token=token, api_calls=run.max_api_calls, orders=1
    )
    assert not await state.reserve_budget(db, actor.tenant_id, run.id, lease_token=token, api_calls=1)
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.api_calls_used == current.max_api_calls
    assert current.status == "finished" and current.termination_reason == "budget"
    assert not await state.reserve_budget(db, actor.tenant_id, run.id, lease_token=token, orders=1)


async def test_wrong_lease_cannot_spend_or_update_progress(db, setup_state):
    actor, _, run = setup_state
    await state.claim_run(db, actor.tenant_id, run.id)
    with pytest.raises(state.StateError, match="run_lease_lost"):
        await state.reserve_budget(db, actor.tenant_id, run.id, lease_token=uuid4(), api_calls=1)
    assert (await state.get_run(db, actor.tenant_id, run.id)).api_calls_used == 0


async def test_deadline_terminates_instead_of_leaving_an_expired_lease(db, setup_state):
    actor, _, run = setup_state
    token = await state.claim_run(db, actor.tenant_id, run.id)
    assert not await state.reserve_budget(
        db, actor.tenant_id, run.id, lease_token=token, api_calls=1, now=run.deadline_at
    )
    assert (await state.get_run(db, actor.tenant_id, run.id)).termination_reason == "budget"


async def test_owner_can_finalize_budget_after_provider_read_reaches_deadline(db, setup_state):
    actor, _, run = setup_state
    token = await state.claim_run(db, actor.tenant_id, run.id)
    result = await state.finish_run(db, actor.tenant_id, run.id, "budget", lease_token=token, now=run.deadline_at)
    assert result.status == "finished" and result.termination_reason == "budget"
    assert result.lease_token is None


async def test_deadline_finalization_does_not_allow_another_owner_or_success(db, setup_state):
    actor, _, run = setup_state
    token = await state.claim_run(db, actor.tenant_id, run.id)
    for reason, supplied_token in (("done", token), ("budget", uuid4())):
        with pytest.raises(state.StateError, match="run_lease_lost"):
            await state.finish_run(db, actor.tenant_id, run.id, reason, lease_token=supplied_token, now=run.deadline_at)
    assert (await state.get_run(db, actor.tenant_id, run.id)).status == "running"


async def test_expired_lease_before_deadline_cannot_finalize_budget(db, setup_state):
    actor, _, run = setup_state
    token = await state.claim_run(db, actor.tenant_id, run.id)
    with pytest.raises(state.StateError, match="run_lease_lost"):
        await state.finish_run(db, actor.tenant_id, run.id, "budget", lease_token=token, now=run.lease_until)


async def test_reports_survive_and_continuation_preserves_bounded_cursor(db, setup_state):
    actor, config, run = setup_state
    token = await state.claim_run(db, actor.tenant_id, run.id)
    await state.record_finding(db, actor.tenant_id, run.id, "R123456789", {"action": "no_action"}, lease_token=token)
    assert (await state.list_findings(db, actor.tenant_id, run.id))[0].report_json == {"action": "no_action"}
    await state.update_progress(
        db, actor.tenant_id, run.id, ProgressUpdate(progress_json={"next_page": 3}), lease_token=token
    )
    await state.finish_run(db, actor.tenant_id, run.id, "budget", lease_token=token)
    resumed = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="next-occurrence", order_references=["R123456789"]),
        actor=actor,
        resume_from_run_id=run.id,
    )
    assert resumed.progress_json == {"next_page": 3}
    assert resumed.api_calls_used == 0
    assert resumed.id != run.id


async def test_schedules_are_rejected_until_human_explicitly_enables(db, setup_state):
    actor, config, _ = setup_state
    request = RunCreate(origin="schedule", evaluation_key="schedule-1", order_references=["R123456789"])
    with pytest.raises(state.StateError, match="schedule_disabled"):
        await state.create_run(db, actor.tenant_id, config.id, request)
    await state.control_config(
        db, actor.tenant_id, config.id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
    )
    assert (await state.create_run(db, actor.tenant_id, config.id, request)).origin == "schedule"


async def test_unknown_outcome_never_reacquires_and_ledger_precedes_dispatch(db, setup_state):
    actor, _, run = setup_state
    proposal = await new_proposal(db, actor, run)
    with pytest.raises(state.StateError, match="proposal_not_approved"):
        await state.claim_approved_operation(db, actor.tenant_id, proposal.id, expected_evidence_fingerprint="a" * 64)
    await state.decide_proposal(
        db,
        actor.tenant_id,
        proposal.id,
        ProposalDecision(decision="approve", evidence_fingerprint="a" * 64),
        actor=actor,
    )
    claim = await state.claim_approved_operation(
        db, actor.tenant_id, proposal.id, expected_evidence_fingerprint="a" * 64
    )
    assert claim.after_json == {"total": "121.00"}
    operation = (
        await db.execute(select(TransactionOperation).where(TransactionOperation.id == claim.operation_id))
    ).scalar_one()
    assert operation.status == "executing"
    await state.complete_operation(
        db, actor.tenant_id, operation.id, outcome="unknown", result_json={"reason": "timeout"}
    )
    with pytest.raises(state.StateError, match="operation_already_attempted"):
        await state.claim_approved_operation(db, actor.tenant_id, proposal.id, expected_evidence_fingerprint="a" * 64)


async def test_revalidation_invalidates_approval_before_any_attempt(db, setup_state):
    actor, _, run = setup_state
    proposal = await new_proposal(db, actor, run)
    await state.decide_proposal(
        db,
        actor.tenant_id,
        proposal.id,
        ProposalDecision(decision="approve", evidence_fingerprint="a" * 64),
        actor=actor,
    )
    assert (
        await state.claim_approved_operation(db, actor.tenant_id, proposal.id, expected_evidence_fingerprint="b" * 64)
        is None
    )
    await db.refresh(proposal)
    assert proposal.status == "superseded"
    assert (
        await db.execute(select(TransactionOperation).where(TransactionOperation.proposal_id == proposal.id))
    ).scalar_one_or_none() is None


async def test_cross_tenant_reads_and_decisions_are_not_found(db, setup_state, tenant_b):
    actor, _, run = setup_state
    proposal = await new_proposal(db, actor, run)
    with pytest.raises(state.StateError, match="not_found"):
        await state.get_run(db, tenant_b.id, run.id)
    with pytest.raises(state.StateError, match="not_found"):
        await state.get_proposal(db, tenant_b.id, proposal.id)


async def test_old_evidence_cannot_receive_approval(db, setup_state):
    actor, _, run = setup_state
    old = proposal_input(observed_at=datetime.now(timezone.utc) - timedelta(minutes=16))
    token = await state.claim_run(db, actor.tenant_id, run.id)
    with pytest.raises(state.StateError, match="stale_evidence"):
        await state.propose(db, actor.tenant_id, run.id, old, lease_token=token)


async def test_fresh_reread_after_expiry_creates_new_pending_approval_same_work(db, setup_state):
    actor, config, run = setup_state
    now = datetime.now(timezone.utc)
    token = await state.claim_run(db, actor.tenant_id, run.id, now=now)
    first = await state.propose(
        db, actor.tenant_id, run.id, proposal_input(observed_at=now), now=now, lease_token=token
    )
    later = now + timedelta(minutes=16)
    fresh_run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="fresh-reread", order_references=["R123456789"]),
        actor=actor,
        now=later,
    )
    fresh_token = await state.claim_run(db, actor.tenant_id, fresh_run.id, now=later)
    second = await state.propose(
        db, actor.tenant_id, fresh_run.id, proposal_input(observed_at=later), now=later, lease_token=fresh_token
    )
    assert first.id != second.id
    assert first.work_key == second.work_key
    assert (await state.get_proposal(db, actor.tenant_id, first.id)).status == "superseded"
    assert second.status == "pending"


@pytest.mark.parametrize("revoke", ["inactive", "agent", "permission"])
async def test_claim_invalidates_human_approval_when_actor_is_revoked(db, setup_state, revoke):
    actor, _, run = setup_state
    proposal = await new_proposal(db, actor, run)
    await state.decide_proposal(
        db,
        actor.tenant_id,
        proposal.id,
        ProposalDecision(decision="approve", evidence_fingerprint="a" * 64),
        actor=actor,
    )
    if revoke == "permission":
        await db.execute(text("DELETE FROM user_roles WHERE user_id=:id"), {"id": actor.id})
    else:
        field, value = ("is_active", False) if revoke == "inactive" else ("actor_type", "agent")
        await db.execute(text(f"UPDATE users SET {field}=:value WHERE id=:id"), {"value": value, "id": actor.id})
    assert (
        await state.claim_approved_operation(db, actor.tenant_id, proposal.id, expected_evidence_fingerprint="a" * 64)
        is None
    )
    assert (await state.get_proposal(db, actor.tenant_id, proposal.id)).status == "superseded"


async def test_abandoned_worker_cannot_create_proposals(db, setup_state):
    actor, _, run = setup_state
    token = await state.claim_run(db, actor.tenant_id, run.id)
    now = datetime.now(timezone.utc) + timedelta(seconds=181)
    with pytest.raises(state.StateError, match="run_lease_lost"):
        await state.propose(db, actor.tenant_id, run.id, proposal_input(observed_at=now), now=now, lease_token=token)


async def test_unknown_attempt_blocks_changed_proposals_for_same_order(db, setup_state):
    actor, _, run = setup_state
    first = await new_proposal(db, actor, run)
    await state.decide_proposal(
        db, actor.tenant_id, first.id, ProposalDecision(decision="approve", evidence_fingerprint="a" * 64), actor=actor
    )
    attempt = await state.claim_approved_operation(
        db, actor.tenant_id, first.id, expected_evidence_fingerprint="a" * 64
    )
    await state.complete_operation(
        db, actor.tenant_id, attempt.operation_id, outcome="unknown", result_json={"reason": "timeout"}
    )
    second = await new_proposal(db, actor, run, evidence_fingerprint="b" * 64, after_json={"total": "122.00"})
    await state.decide_proposal(
        db, actor.tenant_id, second.id, ProposalDecision(decision="approve", evidence_fingerprint="b" * 64), actor=actor
    )
    with pytest.raises(state.StateError, match="operation_already_attempted"):
        await state.claim_approved_operation(db, actor.tenant_id, second.id, expected_evidence_fingerprint="b" * 64)


async def test_rejected_proposal_cannot_be_approved_later(db, setup_state):
    actor, _, run = setup_state
    proposal = await new_proposal(db, actor, run)
    await state.decide_proposal(
        db,
        actor.tenant_id,
        proposal.id,
        ProposalDecision(decision="reject", evidence_fingerprint="a" * 64),
        actor=actor,
    )
    with pytest.raises(state.StateError, match="proposal_not_pending"):
        await state.decide_proposal(
            db,
            actor.tenant_id,
            proposal.id,
            ProposalDecision(decision="approve", evidence_fingerprint="a" * 64),
            actor=actor,
        )


async def test_persisted_proposal_is_immutable_even_via_sql(db, setup_state):
    actor, _, run = setup_state
    proposal = await new_proposal(db, actor, run)
    with pytest.raises(DBAPIError, match="immutable"):
        async with db.begin_nested():
            await db.execute(
                text("UPDATE transaction_ops_proposals SET after_json='{}' WHERE id=:id"), {"id": proposal.id}
            )


async def test_rls_enforces_read_and_write_scope_without_superuser_bypass(db, setup_state, tenant_b):
    from app.core.database import set_tenant_context

    actor, config, run = setup_state
    role = f"tx_rls_{uuid4().hex[:12]}"  # Generated identifier, no user input.
    await db.execute(text(f"CREATE ROLE {role} NOLOGIN"))
    await db.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    await db.execute(text(f"GRANT SELECT ON transaction_ops_configs TO {role}"))
    await db.execute(text(f"GRANT INSERT ON transaction_ops_findings TO {role}"))
    await db.execute(text(f"SET LOCAL ROLE {role}"))
    try:
        await set_tenant_context(db, str(actor.tenant_id))
        assert (await db.execute(text("SELECT id FROM transaction_ops_configs"))).scalars().all() == [config.id]
        await set_tenant_context(db, str(tenant_b.id))
        assert (await db.execute(text("SELECT id FROM transaction_ops_configs"))).scalars().all() == []
        with pytest.raises(DBAPIError, match="row-level security"):
            async with db.begin_nested():
                await db.execute(
                    text(
                        "INSERT INTO transaction_ops_findings (id,tenant_id,run_id,order_reference,report_json) VALUES (:id,:tenant,:run,'R123456789','{}')"
                    ),
                    {"id": uuid4(), "tenant": actor.tenant_id, "run": run.id},
                )
    finally:
        await db.execute(text("RESET ROLE"))
        await set_tenant_context(db, str(actor.tenant_id))


async def test_claim_commits_attempt_before_return_and_restores_rls_context(db, setup_state, monkeypatch):
    actor, _, run = setup_state
    proposal = await new_proposal(db, actor, run)
    await state.decide_proposal(
        db,
        actor.tenant_id,
        proposal.id,
        ProposalDecision(decision="approve", evidence_fingerprint="a" * 64),
        actor=actor,
    )
    events = []
    original_commit = db.commit
    original_context = state.set_tenant_context

    async def commit():
        operation = (
            await db.execute(select(TransactionOperation).where(TransactionOperation.proposal_id == proposal.id))
        ).scalar_one()
        assert operation.status == "executing"
        events.append("commit_attempt")
        await original_commit()

    async def context(session, tenant_id):
        events.append("tenant_context")
        await original_context(session, tenant_id)

    monkeypatch.setattr(db, "commit", commit)
    monkeypatch.setattr(state, "set_tenant_context", context)
    intent = await state.claim_approved_operation(
        db, actor.tenant_id, proposal.id, expected_evidence_fingerprint="a" * 64
    )
    assert intent is not None
    assert events[-2:] == ["commit_attempt", "tenant_context"]


async def test_expired_lease_recovery_preserves_spend_and_old_worker_cannot_write(db, setup_state):
    actor, _, run = setup_state
    now = datetime.now(timezone.utc)
    first = await state.claim_run(db, actor.tenant_id, run.id, now=now)
    await state.reserve_budget(db, actor.tenant_id, run.id, lease_token=first, api_calls=2, now=now)
    later = now + timedelta(seconds=181)
    second = await state.claim_run(db, actor.tenant_id, run.id, now=later)
    assert second != first
    assert (await state.get_run(db, actor.tenant_id, run.id)).api_calls_used == 2
    with pytest.raises(state.StateError, match="run_lease_lost"):
        await state.record_finding(db, actor.tenant_id, run.id, "R123456789", {}, lease_token=first, now=later)


async def test_budget_and_config_mapping_cannot_be_rewritten_in_storage(db, setup_state):
    actor, config, run = setup_state
    for statement, identifier in (
        ("UPDATE transaction_ops_configs SET mapping_json='{}' WHERE id=:id", config.id),
        ("UPDATE transaction_ops_runs SET max_api_calls=2000 WHERE id=:id", run.id),
        ("UPDATE transaction_ops_runs SET params_json='{}' WHERE id=:id", run.id),
    ):
        with pytest.raises(DBAPIError, match="immutable"):
            async with db.begin_nested():
                await db.execute(text(statement), {"id": identifier})


async def test_approval_actor_comes_from_current_human_and_is_audited(db, setup_state):
    actor, _, run = setup_state
    proposal = await new_proposal(db, actor, run)
    decision = ProposalDecision(decision="approve", evidence_fingerprint="a" * 64, note="Checked source")
    await state.decide_proposal(db, actor.tenant_id, proposal.id, decision, actor=actor)
    assert proposal.decided_by == actor.id
    event = (
        await db.execute(
            text("SELECT actor_id FROM audit_events WHERE resource_id=:id AND action='transaction_ops.approve'"),
            {"id": str(proposal.id)},
        )
    ).scalar_one()
    assert event == actor.id
    second = await new_proposal(db, actor, run, evidence_fingerprint="b" * 64)
    actor.actor_type = "agent"
    await db.flush()
    with pytest.raises(state.StateError, match="human_actor_required"):
        await state.decide_proposal(
            db,
            actor.tenant_id,
            second.id,
            ProposalDecision(decision="approve", evidence_fingerprint="b" * 64),
            actor=actor,
        )


async def test_operation_audit_links_exact_proposal_and_human_decision(db, setup_state):
    from app.models.audit import AuditEvent

    actor, config, run = setup_state
    proposal = await new_proposal(db, actor, run)
    await state.decide_proposal(
        db,
        actor.tenant_id,
        proposal.id,
        ProposalDecision(decision="approve", evidence_fingerprint="a" * 64, note="Reviewed exact changes"),
        actor=actor,
    )
    claim = await state.claim_approved_operation(
        db, actor.tenant_id, proposal.id, expected_evidence_fingerprint="a" * 64
    )
    await state.complete_operation(
        db,
        actor.tenant_id,
        claim.operation_id,
        outcome="verified",
        result_json={"code": "independently_verified", "verification": {"source_unchanged": True}},
    )
    events = list(
        await db.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == actor.tenant_id,
                AuditEvent.resource_id.in_([str(proposal.id), str(claim.operation_id)]),
            )
        )
    )
    by_action = {event.action: event for event in events}
    created = by_action["transaction_ops.proposal.create"]
    assert created.payload["run_id"] == str(run.id)
    assert created.payload["config_id"] == str(config.id)
    assert created.payload["proposal_id"] == str(proposal.id)
    approved = by_action["transaction_ops.approve"]
    assert approved.actor_id == actor.id
    assert approved.payload["decided_by"] == str(actor.id)
    assert approved.payload["decided_at"] == proposal.decided_at.isoformat()
    for name in ("operation.attempt", "operation.complete"):
        event = by_action[f"transaction_ops.{name}"]
        assert event.actor_type == "system" and event.actor_id is None
        assert event.payload["proposal_id"] == str(proposal.id)
        assert event.payload["operation_id"] == str(claim.operation_id)
    completed = by_action["transaction_ops.operation.complete"]
    assert completed.payload["approved_by"] == str(actor.id)
    assert completed.payload["approved_at"] == proposal.decided_at.isoformat()
    assert completed.payload["evidence_fingerprint"] == proposal.evidence_fingerprint
    assert completed.payload["outcome"] == "verified"
    # Verifying the approved write is not proof of full financial reconciliation.
    assert completed.payload["settlement_status"] == "not_evaluated"


@pytest.mark.parametrize(
    "mapping", [{"reference_field": "tranid"}, {"reference_field": "tranid", "action_mode": "detect_only"}]
)
async def test_detect_only_scope_cannot_produce_a_proposal(db, admin_user, mapping):
    actor, _ = admin_user
    config = await seed_config(db, actor.tenant_id, actor, mapping_json=mapping)
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="detect-only", order_references=["R123456789"]),
        actor=actor,
    )
    with pytest.raises(state.StateError, match="actions_disabled"):
        await new_proposal(db, actor, run)
