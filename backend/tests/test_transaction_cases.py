from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import case_service
from app.services.transaction_ops import state_service as state
from tests.test_transaction_ops_state_db import seed_config

REF = "R123456789"
NOW = datetime.now(timezone.utc)


def report(status="mismatch", observed=None):
    now = (observed or NOW).isoformat()
    return {
        "order_reference": REF,
        "source": {"observed_at": now, "authoritative": True},
        "targets": [{"observed_at": now, "authoritative": True}],
        "lookup": {"complete": True, "authoritative": True},
        "comparison": {"recommended_action": "no_action" if status == "matched" else "human_review", "findings": []},
        "balance": {
            "status": status,
            "currency": "USD",
            "source_observed_at": now,
            "target_observed_at": now,
            "missing_metrics": [],
            "amounts": {
                key: {
                    "source": "100.00",
                    "target": "100.00" if status == "matched" else "99.00",
                    "delta": "0.00" if status == "matched" else "1.00",
                }
                for key in ("order_total", "tax", "refunds")
            },
        },
    }


async def observe(db, actor, config, body, now):
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key=str(uuid4()), order_references=[REF]),
        actor=actor,
        now=now,
    )
    token = await state.claim_run(db, actor.tenant_id, run.id, now=now)
    finding = await state.record_finding(db, actor.tenant_id, run.id, REF, body, lease_token=token, now=now)
    await state.finish_run(db, actor.tenant_id, run.id, "done", lease_token=token, now=now)
    return finding


@pytest.mark.asyncio
async def test_case_carries_across_runs_reopens_and_preserves_observations(db, admin_user):
    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    finding = await observe(db, actor, config, report(), NOW)
    first = (await case_service.list_cases(db, actor.tenant_id))[0]
    assert first.status == "open"
    assert finding.report_json["case_id"] == str(first.id)
    await observe(db, actor, config, report("matched"), NOW + timedelta(seconds=1))
    second = (await case_service.list_cases(db, actor.tenant_id))[0]
    assert second.id == first.id and second.status == "reconciled"
    await observe(db, actor, config, report(), NOW + timedelta(seconds=2))
    assert (await case_service.get_case(db, actor.tenant_id, first.id)).status == "open"
    observations = await case_service.list_observations(db, actor.tenant_id, first.id)
    assert len(observations) == 3
    with pytest.raises(DBAPIError, match="immutable"):
        async with db.begin_nested():
            await db.execute(
                text("UPDATE transaction_case_observations SET report_json='{}' WHERE id=:id"),
                {"id": observations[0].id},
            )


async def test_late_old_evidence_cannot_reopen_reconciled_case_but_new_discrepancy_can(db, admin_user):
    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    await observe(db, actor, config, report(observed=NOW), NOW)
    case = (await case_service.list_cases(db, actor.tenant_id))[0]
    matched_at = NOW + timedelta(minutes=1)
    await observe(db, actor, config, report("matched", matched_at), matched_at)
    # An overlapping daily job finishes after the correction's readback but
    # carries the original, pre-correction snapshots.
    await observe(db, actor, config, report(observed=NOW), matched_at + timedelta(minutes=1))
    assert (await case_service.get_case(db, actor.tenant_id, case.id)).status == "reconciled"
    assert case.latest_report_json["balance"]["status"] == "matched"
    assert len(await case_service.list_observations(db, actor.tenant_id, case.id)) == 3
    # Repeated fresh reads of the same corrected amounts do not create a case.
    later = NOW + timedelta(minutes=3)
    await observe(db, actor, config, report("matched", later), later)
    assert await case_service.list_cases(db, actor.tenant_id, status="open") == []
    assert len(await case_service.list_cases(db, actor.tenant_id)) == 1
    changed = later + timedelta(minutes=1)
    await observe(db, actor, config, report(observed=changed), changed)
    assert (await case_service.get_case(db, actor.tenant_id, case.id)).status == "open"


async def test_finding_observation_marker_is_server_owned(db, admin_user):
    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    body = report()
    body["_observation"] = {"final": False, "observed_at": "3000-01-01T00:00:00Z"}
    finding = await observe(db, actor, config, body, NOW)
    assert finding.report_json["_observation"] == {"final": True, "observed_at": NOW.isoformat()}


async def test_repeated_scans_apply_existing_net_and_tax_credit_without_reopening_case(db, admin_user):
    from app.services.transaction_ops.order_reconciliation import reconcile_order
    from tests.test_refund_adjustment_matching import balance_case

    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    source, target, scope, refunds = balance_case()
    source["orders"][0]["included_tax_total"] = "18"
    proof = refunds["target"]["tax_adjustments"][0]
    proof.update(kind="credit_memo", reason_id="3", tax_amount="0", item_accounts={"81": "91"})
    # Before correction the gross refund agrees, but its tax allocation does not.
    for minute, tax in enumerate(["0", "2", "2", "2", "1"]):
        now = NOW + timedelta(minutes=minute)
        source["read_at"] = target["observed_at"] = now.isoformat()
        proof["tax_amount"] = tax
        body = report(observed=now)
        body["balance"] = reconcile_order(source, target, scope, refunds=refunds)
        await observe(db, actor, config, body, now)
        cases = await case_service.list_cases(db, actor.tenant_id)
        assert len(cases) == 1
        assert cases[0].status == ("reconciled" if tax == "2" else "open")
        assert body["balance"]["amounts"]["refunds"]["delta"] == "0.00"


@pytest.mark.asyncio
async def test_stale_or_partial_match_cannot_close_case_and_other_tenant_cannot_read(db, admin_user, tenant_b):
    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    await observe(db, actor, config, report(), NOW)
    c = (await case_service.list_cases(db, actor.tenant_id))[0]
    await observe(db, actor, config, report("matched", NOW - timedelta(days=1)), NOW + timedelta(seconds=1))
    assert (await case_service.get_case(db, actor.tenant_id, c.id)).status == "open"
    assert not await case_service.list_cases(db, tenant_b.id)
    with pytest.raises(state.StateError, match="not_found"):
        await case_service.list_observations(db, tenant_b.id, c.id)


@pytest.mark.asyncio
async def test_unknown_write_keeps_case_open_despite_matching_amounts(db, admin_user):
    from app.schemas.transaction_runs import ProposalDecision
    from tests.test_transaction_ops_state_db import new_proposal

    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    run = await state.create_run(
        db, actor.tenant_id, config.id, RunCreate(evaluation_key="unknown", order_references=[REF]), actor=actor
    )
    proposal = await new_proposal(db, actor, run)
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
    await state.complete_operation(
        db, actor.tenant_id, claim.operation_id, outcome="unknown", result_json={"reason": "timeout"}
    )
    await observe(db, actor, config, report(), NOW + timedelta(seconds=3))
    await observe(db, actor, config, report("matched"), NOW + timedelta(seconds=4))
    assert (await case_service.list_cases(db, actor.tenant_id))[0].status == "open"


@pytest.mark.asyncio
async def test_same_finding_retry_does_not_duplicate_history(db, admin_user):
    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    run = await state.create_run(
        db, actor.tenant_id, config.id, RunCreate(evaluation_key="retry", order_references=[REF]), actor=actor, now=NOW
    )
    token = await state.claim_run(db, actor.tenant_id, run.id, now=NOW)
    for _ in range(2):
        await state.record_finding(db, actor.tenant_id, run.id, REF, report(), lease_token=token, now=NOW)
    c = (await case_service.list_cases(db, actor.tenant_id))[0]
    assert len(await case_service.list_observations(db, actor.tenant_id, c.id)) == 1


@pytest.mark.asyncio
async def test_case_is_stable_across_config_revision(db, admin_user, monkeypatch):
    from unittest.mock import AsyncMock

    from app.services.transaction_ops import replica_setup
    from tests.test_metabase_replica_reader import BINDING
    from tests.test_transaction_replica_setup import connector

    actor = admin_user[0]
    old = await seed_config(db, actor.tenant_id, actor)
    await observe(db, actor, old, report(), NOW)
    case = (await case_service.list_cases(db, actor.tenant_id))[0]
    c = await connector(db, actor)
    monkeypatch.setattr(replica_setup.metabase_reader, "read_order_page", AsyncMock(return_value={"orders": []}))
    new = (
        await replica_setup.bind_verified_replica(
            db, actor.tenant_id, [old.id], {**BINDING, "connector_id": str(c.id)}, actor=actor
        )
    )[0]
    await observe(db, actor, new, report(), NOW + timedelta(seconds=2))
    assert [r.id for r in await case_service.list_cases(db, actor.tenant_id)] == [case.id]
    assert len(await case_service.list_observations(db, actor.tenant_id, case.id)) == 2


def test_equal_closed_order_is_reconciled_without_becoming_repair_eligible():
    evidence = report("matched")
    evidence["comparison"] = {
        "recommended_action": "human_review",
        "differences": [],
        "findings": [
            {"code": "record_state_requires_review", "reason": "Closed/refunded order requires review before writes"}
        ],
    }
    assert case_service._cleared(evidence, NOW)
    assert evidence["comparison"]["recommended_action"] == "human_review"


@pytest.mark.asyncio
async def test_case_rls_blocks_direct_cross_tenant_reads_and_inserts(db, admin_user, tenant_b):
    from app.core.database import set_tenant_context

    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    finding = await observe(db, actor, config, report(), NOW)
    case = (await case_service.list_cases(db, actor.tenant_id))[0]
    role = f"case_rls_{uuid4().hex[:12]}"
    await db.execute(text(f"CREATE ROLE {role} NOLOGIN"))
    await db.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    await db.execute(text(f"GRANT SELECT,INSERT ON transaction_cases,transaction_case_observations TO {role}"))
    await db.execute(text(f"SET LOCAL ROLE {role}"))
    try:
        await set_tenant_context(db, str(tenant_b.id))
        for table in ("transaction_cases", "transaction_case_observations"):
            assert not (await db.execute(text(f"SELECT id FROM {table}"))).scalars().all()
        with pytest.raises(DBAPIError, match="row-level security"):
            async with db.begin_nested():
                await db.execute(
                    text(
                        "INSERT INTO transaction_case_observations (id,tenant_id,case_id,run_id,observation_key,observed_at,report_json) VALUES (:id,:tenant,:case,:run,:key,now(),'{}')"
                    ),
                    {"id": uuid4(), "tenant": actor.tenant_id, "case": case.id, "run": finding.run_id, "key": "c" * 64},
                )
    finally:
        await db.execute(text("RESET ROLE"))
        await set_tenant_context(db, str(actor.tenant_id))


@pytest.mark.asyncio
async def test_finding_cannot_attach_another_order_or_caller_case_id(db, admin_user):
    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    body = report()
    body["order_reference"] = "R999999999"
    with pytest.raises(state.StateError, match="finding_order_mismatch"):
        await observe(db, actor, config, body, NOW)
    body = report()
    body["case_id"] = str(uuid4())
    finding = await observe(db, actor, config, body, NOW)
    assert finding.report_json["case_id"] != body["case_id"]


@pytest.mark.asyncio
async def test_intermediate_refund_collection_does_not_open_an_exception(db, admin_user):
    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="collecting", order_references=[REF]),
        actor=actor,
        now=NOW,
    )
    token = await state.claim_run(db, actor.tenant_id, run.id, now=NOW)
    body = report("incomplete")
    body["balance"]["missing_metrics"] = ["refunds"]
    await state.record_finding(db, actor.tenant_id, run.id, REF, body, lease_token=token, now=NOW, final=False)
    assert not await case_service.list_cases(db, actor.tenant_id)
    assert len(await state.list_findings(db, actor.tenant_id, run.id)) == 1
    await state.record_finding(db, actor.tenant_id, run.id, REF, report("matched"), lease_token=token, now=NOW)
    assert not await case_service.list_cases(db, actor.tenant_id)


@pytest.mark.parametrize("limits", [None, {"code": "detailed_evidence_unavailable"}, {"code": "evidence_size_limit"}])
def test_proven_financial_match_clears_case_without_granting_write_readiness(limits):
    evidence = report("matched")
    evidence["comparison"] = {
        "recommended_action": "gather_evidence",
        "findings": [{"code": "tax_detail_incomplete"}],
        "differences": [{"field": "tax", "source": "2", "target": "0"}],
    }
    if limits:
        evidence["evidence_limits"] = limits
    assert case_service._cleared(evidence, NOW)
    assert evidence["comparison"]["recommended_action"] == "gather_evidence"


def test_unknown_coverage_limit_cannot_close_financial_case():
    evidence = report("matched")
    evidence["evidence_limits"] = {"code": "refund_page_incomplete"}
    assert not case_service._cleared(evidence, NOW)


@pytest.mark.asyncio
async def test_every_case_evaluation_is_audited_and_penny_difference_stays_open(db, admin_user):
    from sqlalchemy import select

    from app.models.audit import AuditEvent

    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    penny = report("matched")
    penny["balance"]["amounts"]["tax"] = {"source": "0.55", "target": "0.56", "delta": "-0.01"}
    first = await observe(db, actor, config, penny, NOW)
    case = (await case_service.list_cases(db, actor.tenant_id))[0]
    assert case.status == "open"
    await observe(db, actor, config, penny, NOW + timedelta(seconds=1))
    await observe(db, actor, config, report("matched"), NOW + timedelta(seconds=2))
    events = list(
        await db.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == actor.tenant_id,
                AuditEvent.resource_id == str(case.id),
                AuditEvent.action == "transaction_ops.case.evaluated",
            )
        )
    )
    assert len(events) == 3
    by_run = {event.payload["run_id"]: event for event in events}
    assert by_run[str(first.run_id)].payload["reconciliation_verified"] is False
    assert sum(event.payload["reconciliation_verified"] is True for event in events) == 1
    observations = await case_service.list_observations(db, actor.tenant_id, case.id)
    assert {event.payload["observation_id"] for event in events} == {str(row.id) for row in observations}
    assert all(event.actor_type == "system" for event in events)


@pytest.mark.parametrize("prior_status,late_status", [("matched", "difference"), ("difference", "matched")])
async def test_prefetched_refund_cannot_replace_newer_financial_observation(db, admin_user, prior_status, late_status):
    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    await observe(db, actor, config, report(observed=NOW), NOW)
    case = (await case_service.list_cases(db, actor.tenant_id))[0]
    current = NOW + timedelta(minutes=1)
    await observe(db, actor, config, report(prior_status, current), current)
    late_at = NOW + timedelta(minutes=2)
    late = report(late_status, late_at)
    late["refund_evidence"] = {
        "source": {"observed_at": NOW.isoformat()},
        "target": {"observed_at": late_at.isoformat()},
    }
    finding = await observe(db, actor, config, late, late_at)
    assert case.latest_report_json["balance"]["status"] == prior_status
    assert finding.report_json["_observation"]["observed_at"] == NOW.isoformat()


@pytest.mark.parametrize("age", [timedelta(minutes=16), timedelta(seconds=-1)])
@pytest.mark.parametrize("compacted", [False, True])
def test_refund_freshness_controls_case_clearing_even_in_compact_reports(age, compacted):
    body = report("matched", NOW)
    observed = (NOW - age).isoformat()
    if compacted:
        body["refund_observation_times"] = [observed]
    else:
        body["refund_evidence"] = {"source": {"observed_at": observed}}
    assert not case_service._cleared(body, NOW)
