from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import update

from app.models.feature_flag import TenantFeatureFlag
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import continuation, framework_defaults, state_service
from tests.test_transaction_defaults import connections


async def budget_run(db, user, *, reason="budget", progress=None):
    await db.execute(
        update(TenantFeatureFlag)
        .where(
            TenantFeatureFlag.tenant_id == user.tenant_id, TenantFeatureFlag.flag_key.in_(("celigo", "reconciliation"))
        )
        .values(enabled=True)
    )
    source, _ = await connections(db, user.tenant_id)
    config = (await framework_defaults.ensure_framework_configs(db, user.tenant_id, source.id, actor=user))[0]
    run = await state_service.create_run(
        db,
        user.tenant_id,
        config.id,
        RunCreate(evaluation_key=str(uuid4()), order_references=("R100120031", "R100120032")),
        actor=user,
    )
    run.status, run.termination_reason = "finished", reason
    run.finished_at = datetime.now(timezone.utc)
    run.progress_json = {
        "processed": 1,
        "scan_count": 0,
        "pending_refs": ["R100120032"],
        "scan_complete": True,
        **(progress or {}),
    }
    await db.flush()
    return run, config


async def test_budget_continuation_keeps_scope_cursor_and_human_provenance(db, admin_user):
    user, _ = admin_user
    prior, _ = await budget_run(db, user)
    child = await continuation.continue_budget_run(db, user.tenant_id, prior.id)
    assert child.id != prior.id and child.initiated_by == user.id and child.origin == "manual"
    assert child.params_json["order_references"] == prior.params_json["order_references"]
    assert child.progress_json["pending_refs"] == ["R100120032"]
    assert child.progress_json["continuation_part"] == 2
    assert child.progress_json["continuation_baseline"] == {"processed": 1, "scan_count": 0}
    assert (await continuation.continuation_result(db, user.tenant_id, prior.id))[0].id == child.id
    again = await continuation.continue_budget_run(db, user.tenant_id, prior.id)
    assert again.id == child.id
    assert "continuation_run_id" not in prior.progress_json  # Terminal evidence remains immutable.


@pytest.mark.parametrize("reason", ["error", "stall", "done"])
async def test_failures_and_complete_runs_do_not_reset_their_budget(db, admin_user, reason):
    user, _ = admin_user
    run, _ = await budget_run(db, user, reason=reason)
    assert await continuation.continue_budget_run(db, user.tenant_id, run.id) is None


@pytest.mark.parametrize("guard", ["no_progress", "part_limit", "paused"])
async def test_automatic_continuations_have_finite_limits_and_require_progress(db, admin_user, guard):
    user, _ = admin_user
    progress = {}
    if guard == "no_progress":
        progress = {"continuation_baseline": {"processed": 1, "scan_count": 0}}
    elif guard == "part_limit":
        progress = {"continuation_part": continuation.MAX_PARTS}
    run, config = await budget_run(db, user, progress=progress)
    if guard == "paused":
        config.enabled = config.schedule_enabled = False
    await db.flush()
    assert await continuation.continue_budget_run(db, user.tenant_id, run.id) is None
    assert (await continuation.continuation_result(db, user.tenant_id, run.id))[1]["reason"]


async def test_revoked_feature_stops_continuation_and_other_tenants_cannot_resume(db, admin_user, admin_user_b):
    user, _ = admin_user
    run, _ = await budget_run(db, user)
    with pytest.raises(state_service.StateError, match="not_found"):
        await continuation.continue_budget_run(db, admin_user_b[0].tenant_id, run.id)
    await db.execute(
        update(TenantFeatureFlag)
        .where(TenantFeatureFlag.tenant_id == user.tenant_id, TenantFeatureFlag.flag_key == "reconciliation")
        .values(enabled=False)
    )
    assert await continuation.continue_budget_run(db, user.tenant_id, run.id) is None
    assert (await continuation.continuation_result(db, user.tenant_id, run.id))[1]["reason"] == "feature_unavailable"


async def test_run_api_links_to_its_continuation_without_changing_terminal_evidence(client, db, admin_user):
    user, headers = admin_user
    prior, _ = await budget_run(db, user)
    child = await continuation.continue_budget_run(db, user.tenant_id, prior.id)
    response = await client.get(f"/api/v1/transaction-ops/runs/{prior.id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["continuation_run_id"] == str(child.id)
    assert "continuation_run_id" not in response.json()["progress_json"]
