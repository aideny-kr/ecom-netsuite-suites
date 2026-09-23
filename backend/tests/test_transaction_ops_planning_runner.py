from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.transaction_ops import TransactionOperation
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.netsuite_actions import NetSuiteActionError
from app.services.transaction_ops.runner import run_investigation
from tests.conftest import enable_feature_flag
from tests.test_transaction_ops_planner import planning_case
from tests.test_transaction_ops_state_db import seed_config


@pytest.fixture
async def planning_run(db, admin_user):
    actor, _ = admin_user
    case = planning_case()
    config = await seed_config(db, actor.tenant_id, actor, subsidiary_id="3", mapping_json=case.config.mapping_json)
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(evaluation_key="planning", order_references=["R123456789"]),
        actor=actor,
    )
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
    return actor, case, config, run


async def run_case(db, setup, guard):
    actor, case, config, run = setup
    return await run_investigation(
        db,
        actor.tenant_id,
        run.id,
        _source_reader=AsyncMock(return_value=case.source),
        _target_reader=AsyncMock(return_value=case.targets),
        _guard_reader=guard,
    )


async def test_runner_persists_a_pending_human_proposal_and_does_not_execute(db, planning_run):
    actor, case, config, run = planning_run

    async def guard(*args):
        current = await state.get_run(db, actor.tenant_id, run.id)
        # Source (2) + NetSuite (10 reserved, less its 7 data calls: the fake sends none) + guard (4), before I/O.
        assert current.api_calls_used == 2 + 10 - 7 + 4
        return case.guard

    result = await run_case(db, planning_run, guard)
    assert result["termination_reason"] == "done"
    proposals = await state.list_proposals(db, actor.tenant_id, run_id=run.id)
    assert len(proposals) == 1 and proposals[0].status == "pending"
    assert proposals[0].currency == "EUR"
    assert (
        not (await db.execute(select(TransactionOperation).where(TransactionOperation.tenant_id == actor.tenant_id)))
        .scalars()
        .all()
    )
    findings = await state.list_findings(db, actor.tenant_id, run.id)
    assert findings[0].report_json["automation"]["proposal_id"] == str(proposals[0].id)


@pytest.mark.parametrize(
    "error,code",
    [
        (RuntimeError("private provider token"), "action_evidence_unavailable"),
        (NetSuiteActionError("guard_connection_unavailable"), "guard_connection_unavailable"),
    ],
)
async def test_guard_failure_preserves_amount_findings_and_explains_no_proposal(db, planning_run, error, code):
    actor, case, config, run = planning_run
    result = await run_case(db, planning_run, AsyncMock(side_effect=error))
    assert result["termination_reason"] == "done"
    assert not await state.list_proposals(db, actor.tenant_id, run_id=run.id)
    report = (await state.list_findings(db, actor.tenant_id, run.id))[0].report_json
    assert report["comparison"]["differences"]
    assert report["automation"] == {"status": "blocked", "code": code}
    assert "private provider" not in str(report)


async def test_matching_order_without_celigo_step_has_one_final_observation(db, planning_run, monkeypatch):
    from tests.test_transaction_ops_planner import matched_case

    actor, _, config, run = planning_run
    case = matched_case()
    assert run.config_snapshot.get("target_step_id") is None
    record = AsyncMock(wraps=state.record_finding)
    monkeypatch.setattr(state, "record_finding", record)
    guard = AsyncMock()
    result = await run_case(db, (actor, case, config, run), guard)
    assert result["termination_reason"] == "done"
    guard.assert_not_awaited()
    record.assert_awaited_once()
    findings = await state.list_findings(db, actor.tenant_id, run.id)
    assert findings[0].report_json["comparison"]["recommended_action"] == "no_action"
    assert findings[0].report_json["_observation"]["final"] is True
    assert not await state.list_proposals(db, actor.tenant_id, run_id=run.id)
    assert "automation" not in findings[0].report_json


async def test_fresh_enablement_query_rejects_flag_or_tenant_revocation(db, planning_run):
    from sqlalchemy import update

    from app.models.feature_flag import TenantFeatureFlag
    from app.models.tenant import Tenant
    from app.services.transaction_ops.runner import enabled

    actor, _, _, _ = planning_run
    assert await enabled(db, actor.tenant_id)
    for key in ("celigo", "reconciliation"):
        await db.execute(
            update(TenantFeatureFlag)
            .where(TenantFeatureFlag.tenant_id == actor.tenant_id, TenantFeatureFlag.flag_key == key)
            .values(enabled=False)
        )
        assert not await enabled(db, actor.tenant_id)
        await db.execute(
            update(TenantFeatureFlag)
            .where(TenantFeatureFlag.tenant_id == actor.tenant_id, TenantFeatureFlag.flag_key == key)
            .values(enabled=True)
        )
    await db.execute(update(Tenant).where(Tenant.id == actor.tenant_id).values(is_active=False))
    assert not await enabled(db, actor.tenant_id)
