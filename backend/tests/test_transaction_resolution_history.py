from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.transaction_ops import TransactionRun
from app.schemas.transaction_runs import ProposalDecision, RunCreate
from app.services.transaction_ops import case_service
from app.services.transaction_ops import state_service as state
from tests.test_transaction_cases import report
from tests.test_transaction_ops_state import proposal_input
from tests.test_transaction_ops_state_db import seed_config


async def case_and_proposal(db, actor, config, ref, *, verified=False, fingerprint="a" * 64):
    now = datetime.now(timezone.utc)
    run = await state.create_run(
        db, actor.tenant_id, config.id, RunCreate(evaluation_key=str(uuid4()), order_references=[ref]), actor=actor
    )
    token = await state.claim_run(db, actor.tenant_id, run.id)
    body = report(observed=now)
    from tests.test_transaction_ops_planner import planning_case

    canonical = planning_case().report
    body["source"] = deepcopy(canonical["source"])
    body["targets"] = deepcopy(canonical["targets"])
    for snapshot in (body["source"], body["targets"][0]):
        snapshot.update(order_reference=ref, currency="USD", observed_at=now.isoformat())
    body["order_reference"] = ref
    body["targets"][0]["record_id"] = "200"
    finding = await state.record_finding(db, actor.tenant_id, run.id, ref, body, lease_token=token)
    case = await case_service.get_case(db, actor.tenant_id, finding.report_json["case_id"])
    proposal = await state.propose(
        db,
        actor.tenant_id,
        run.id,
        proposal_input(
            order_reference=ref,
            currency="USD",
            target_record_id="200",
            observed_at=now,
            evidence_json={"report": body},
            evidence_fingerprint=fingerprint,
        ),
        lease_token=token,
    )
    await state.finish_run(db, actor.tenant_id, run.id, "done", lease_token=token)
    if verified:
        await state.decide_proposal(
            db,
            actor.tenant_id,
            proposal.id,
            ProposalDecision(decision="approve", evidence_fingerprint=proposal.evidence_fingerprint),
            actor=actor,
        )
        claim = await state.claim_approved_operation(
            db, actor.tenant_id, proposal.id, expected_evidence_fingerprint=proposal.evidence_fingerprint
        )
        await state.complete_operation(
            db,
            actor.tenant_id,
            claim.operation_id,
            outcome="verified",
            result_json={"verification": {"source_unchanged": True, "report": body}},
        )
    return case, proposal


async def finish_financial_check(db, actor, proposal):
    from app.models.transaction_ops import TransactionOperation

    operation = await db.scalar(
        select(TransactionOperation).where(
            TransactionOperation.tenant_id == actor.tenant_id, TransactionOperation.proposal_id == proposal.id
        )
    )
    run = await db.scalar(
        select(TransactionRun).where(
            TransactionRun.tenant_id == actor.tenant_id,
            TransactionRun.work_key == state.business_digest({"settlement_operation": operation.id}),
        )
    )
    token = await state.claim_run(db, actor.tenant_id, run.id)
    body = deepcopy(proposal.evidence_json["report"])
    body["balance"] = report("matched")["balance"]
    for snapshot in (body["source"], body["targets"][0]):
        snapshot["observed_at"] = datetime.now(timezone.utc).isoformat()
    body["order_reference"] = proposal.order_reference
    body["targets"][0]["record_id"] = proposal.target_record_id
    await state.record_finding(db, actor.tenant_id, run.id, proposal.order_reference, body, lease_token=token)
    await state.finish_run(db, actor.tenant_id, run.id, "done", lease_token=token)
    assert run.progress_json["settlement"]["status"] == "succeeded"
    return run


async def test_case_history_joins_real_decision_execution_and_separate_settlement(db, admin_user):
    from app.services.transaction_ops.resolution_history import history

    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    case, proposal = await case_and_proposal(db, actor, config, "R111111111", verified=True)
    pending = await history(db, actor.tenant_id, case.id)
    row = pending["resolutions"][0]
    assert row["proposal_id"] == str(proposal.id)
    assert row["approved_by"] == str(actor.id)
    assert row["approved_by_name"] == actor.full_name
    assert row["operation_status"] == "verified"
    assert row["settlement_status"] == "pending"
    assert row["verified_example"] is False
    run = await finish_financial_check(db, actor, proposal)
    result = await history(db, actor.tenant_id, case.id)
    row = result["resolutions"][0]
    assert row["settlement_status"] == "succeeded"
    assert row["settlement_run_id"] == str(run.id)
    assert row["verified_example"] is True
    assert row["requires_new_human_approval"] is True
    assert row["proposal_url"].endswith(str(proposal.id))
    assert not result["truncated"]


async def test_examples_require_same_scope_mapping_issue_and_verified_financial_outcome(db, admin_user, tenant_b):
    from app.services.transaction_ops.resolution_history import history

    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    old, proposal = await case_and_proposal(db, actor, config, "R111111111", verified=True)
    await finish_financial_check(db, actor, proposal)
    current, _ = await case_and_proposal(db, actor, config, "R222222222")
    await case_and_proposal(db, actor, config, "R333333333", verified=True)  # Only operation verified.
    other_config = await seed_config(db, actor.tenant_id, actor)
    _, foreign = await case_and_proposal(db, actor, other_config, "R444444444", verified=True)
    await finish_financial_check(db, actor, foreign)
    result = await history(db, actor.tenant_id, current.id)
    assert [row["proposal_id"] for row in result["examples"]] == [str(proposal.id)]
    assert all(row["requires_new_human_approval"] for row in result["examples"])
    with pytest.raises(state.StateError, match="not_found"):
        await history(db, tenant_b.id, current.id)
    different_issue = {**report(), "order_reference": current.order_reference}
    different_issue["balance"]["missing_metrics"] = ["refunds"]
    # A new immutable observation changes applicability; old history stays saved.
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key=str(uuid4()), order_references=[current.order_reference]),
        actor=actor,
    )
    token = await state.claim_run(db, actor.tenant_id, run.id)
    await state.record_finding(db, actor.tenant_id, run.id, current.order_reference, different_issue, lease_token=token)
    await state.finish_run(db, actor.tenant_id, run.id, "done", lease_token=token)
    assert not (await history(db, actor.tenant_id, current.id))["examples"]
    assert (await history(db, actor.tenant_id, old.id))["resolutions"][0]["verified_example"]


async def test_mapping_revision_and_reopened_case_remove_example_applicability(db, admin_user):
    from app.services.transaction_ops.resolution_history import history
    from tests.test_transaction_ops_state import config_input

    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    old, proposal = await case_and_proposal(db, actor, config, "R111111111", verified=True)
    await finish_financial_check(db, actor, proposal)
    current, _ = await case_and_proposal(db, actor, config, "R222222222")
    assert len((await history(db, actor.tenant_id, current.id))["examples"]) == 1
    revised = await state.create_config(
        db,
        actor.tenant_id,
        config_input(
            source_step_id=config.source_step_id,
            netsuite_connection_id=config.netsuite_connection_id,
            mapping_json={**config.mapping_json, "currency_minor_units": {"GBP": 2, "USD": 2}},
        ),
        actor=actor,
    )
    again, _ = await case_and_proposal(db, actor, revised, "R222222222")
    assert again.id == current.id
    assert not (await history(db, actor.tenant_id, current.id))["examples"]
    # Reopening does not erase the successful financial check, but stops the
    # previous fix from being presented as a currently resolved example.
    await case_and_proposal(db, actor, config, "R111111111", fingerprint="b" * 64)
    rows = (await history(db, actor.tenant_id, old.id))["resolutions"]
    assert any(row["settlement_status"] == "succeeded" for row in rows)
    assert all(row["verified_example"] is False for row in rows)


async def test_resolution_history_http_auth_scope_and_pagination(client, app, db, admin_user, admin_user_b):
    from app.api.v1.transaction_ops import router
    from tests.conftest import enable_feature_flag

    app.include_router(router, prefix="/api/v1")
    actor, headers = admin_user
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
        await enable_feature_flag(db, admin_user_b[0].tenant_id, flag)
    config = await seed_config(db, actor.tenant_id, actor)
    case, first = await case_and_proposal(db, actor, config, "R111111111")
    # A distinct evidence version is a different immutable proposal.
    await state.decide_proposal(
        db,
        actor.tenant_id,
        first.id,
        ProposalDecision(decision="reject", evidence_fingerprint=first.evidence_fingerprint),
        actor=actor,
    )
    await case_and_proposal(db, actor, config, "R111111111", fingerprint="b" * 64)
    url = f"/api/v1/transaction-ops/cases/{case.id}/resolution-history"
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers=admin_user_b[1])).status_code == 404
    first_page = await client.get(url, headers=headers, params={"limit": 1})
    assert first_page.status_code == 200, first_page.text
    assert first_page.json()["truncated"] is True
    next_page = await client.get(url, headers=headers, params={"limit": 1, "offset": first_page.json()["next_offset"]})
    assert next_page.status_code == 200 and next_page.json()["truncated"] is False
    assert first_page.json()["resolutions"][0]["proposal_id"] != next_page.json()["resolutions"][0]["proposal_id"]
    assert (await client.get(url, headers=headers, params={"limit": 1000})).status_code == 422


@pytest.mark.parametrize("change", [None, "source_amount", "target_version"])
@pytest.mark.parametrize("compact", [False, True])
def test_example_attribution_requires_same_verified_records(change, compact):
    from types import SimpleNamespace

    from app.services.transaction_ops.planner import source_fingerprint
    from app.services.transaction_ops.resolution_history import _same_verified_state
    from tests.test_transaction_ops_planner import planning_case

    body = planning_case().report
    if compact:
        target = body["targets"][0]
        observation = {key: val for key, val in target.items() if key not in {"lines", "tax_details"}}
        observation.update(line_count=len(target["lines"]), tax_component_count=len(target["tax_details"]))
        proof = {
            "source_unchanged": True,
            "evidence_retention": "summary_and_digests",
            "source_fingerprint": source_fingerprint(body["source"]),
            "target_observation": observation,
        }
    else:
        proof = {"source_unchanged": True, "report": body}
    current = deepcopy(body)
    for snapshot in (current["source"], current["targets"][0]):
        snapshot["observed_at"] = datetime.now(timezone.utc).isoformat()
    if change == "source_amount":
        current["source"]["total"] = "101.00"
    elif change == "target_version":
        current["targets"][0]["updated_at"] = "2026-09-01T00:00:00+00:00"
    assert _same_verified_state(
        SimpleNamespace(result_json={"verification": proof}), SimpleNamespace(report_json=current)
    ) is (change is None)
