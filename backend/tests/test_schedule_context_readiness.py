"""Context readiness blocks scheduled spending; fixtures approve synthetic context only."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.transaction_ops import TransactionOperation, TransactionProposal
from app.schemas.transaction_runs import ConfigControl, RunCreate
from app.services.transaction_ops import context_provenance, scheduled_detection
from app.services.transaction_ops import state_service as state
from tests.test_context_provenance import approve, decision, draft, propose, read
from tests.test_transaction_ops_state import config_input
from tests.test_transaction_ops_state_db import seed_config


async def selected_config(db, actor, variant="approved"):
    request = draft()
    content = request.model_dump(mode="json", exclude={"expected_version", "key"})
    selection = {
        "scope": request.scope.model_dump(),
        "key": request.key,
        "revision": 2 if variant == "revision" else 1,
        "content_sha256": "b" * 64 if variant == "hash" else state.business_digest(content),
    }
    if variant == "scope":
        selection["scope"]["posting_period_id"] = "101"
    config = await seed_config(
        db, actor.tenant_id, actor, mapping_json={**config_input().mapping_json, "scheduled_context": selection}
    )
    if variant != "missing":
        await propose(db, actor, config, request)
    if variant not in {"missing", "draft"}:
        await approve(db, actor, config)
    if variant == "invalidated":
        await context_provenance.decide_context(
            db,
            actor.tenant_id,
            config.id,
            request.key,
            decision(await read(db, actor, config), kind="invalidate"),
            actor=actor,
        )
    return config, request


@pytest.mark.parametrize("variant", ["missing", "draft", "revision", "hash", "scope", "invalidated"])
async def test_invalid_selection_cannot_enable_schedule(db, admin_user, variant):
    actor = admin_user[0]
    config, _ = await selected_config(db, actor, variant)
    view = await scheduled_detection.context_readiness(db, actor.tenant_id, config)
    assert not view["ready"] and view["renewal_needed"]
    with pytest.raises(state.StateError, match="scheduled_context_requires_review"):
        await state.control_config(
            db, actor.tenant_id, config.id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
        )
    current = await state.get_config(db, actor.tenant_id, config.id)
    assert current.enabled and not current.schedule_enabled
    assert not (await db.scalars(select(TransactionProposal))).all()
    assert not (await db.scalars(select(TransactionOperation))).all()


async def test_expiry_reminder_and_queued_run_stop_before_spend(db, admin_user):
    actor = admin_user[0]
    config, request = await selected_config(db, actor)
    before = request.review_by - timedelta(hours=2)
    view = await scheduled_detection.context_readiness(db, actor.tenant_id, config, now=before)
    assert view == {
        "ready": True,
        "status": "expiring",
        "review_by": request.model_dump(mode="json")["review_by"],
        "renewal_needed": True,
    }
    await state.control_config(
        db, actor.tenant_id, config.id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
    )
    expired = request.review_by + timedelta(seconds=1)
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(origin="schedule", evaluation_key="expired-queued", order_references=["R123456789"]),
        now=expired,
    )
    assert await state.claim_run(db, actor.tenant_id, run.id, now=expired) is None
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.status == "finished" and current.termination_reason == "stall"
    assert current.progress_json["reason"] == "scheduled_context_requires_review"
    assert current.api_calls_used == current.orders_used == 0
    # Pausing must remain available after the selection expires/changes.
    await state.control_config(
        db, actor.tenant_id, config.id, ConfigControl(enabled=True, schedule_enabled=False), actor=actor
    )


async def test_collector_records_context_stall_without_creating_job(db, admin_user, monkeypatch):
    from app.services.transaction_ops import scheduler

    actor = admin_user[0]
    config, request = await selected_config(db, actor)
    await state.control_config(
        db, actor.tenant_id, config.id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
    )
    monkeypatch.setattr(
        scheduler.feature_flag_service, "list_tenants_with_flags", AsyncMock(return_value=[actor.tenant_id])
    )
    monkeypatch.setattr(scheduler, "_refresh_sources", AsyncMock(return_value=0))
    monkeypatch.setattr(scheduler, "_candidate_ids", AsyncMock(return_value=[config.id]))
    monkeypatch.setattr(scheduler, "_recovery_ids", AsyncMock(return_value=[]))
    dispatch = AsyncMock()
    monkeypatch.setattr(scheduler, "_dispatch", dispatch)
    stats = await scheduler.collect_due_runs(db, request.review_by + timedelta(seconds=1))
    assert stats["stalled"] == [{"config_id": str(config.id), "reason": "scheduled_context_requires_review"}]
    assert stats["created"] == 0
    dispatch.assert_not_awaited()


async def test_readiness_api_is_tenant_scoped_and_content_free(app, client, db, admin_user, admin_user_b):
    from app.api.v1.transaction_ops import router
    from tests.conftest import enable_feature_flag

    app.include_router(router, prefix="/api/v1")
    actor, headers = admin_user
    for user in (actor, admin_user_b[0]):
        for flag in ("celigo", "reconciliation"):
            await enable_feature_flag(db, user.tenant_id, flag)
    config, _ = await selected_config(db, actor)
    url = f"/api/v1/transaction-ops/configs/{config.id}/schedule-readiness"
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers=admin_user_b[1])).status_code == 404
    response = await client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["ready"]
    assert "statement" not in response.text and "sources" not in response.text


async def test_selected_scope_requires_paused_setup_and_can_pause(db, admin_user):
    actor = admin_user[0]
    request = draft()
    selection = {
        "scope": request.scope.model_dump(),
        "key": request.key,
        "revision": 1,
        "content_sha256": state.business_digest(request.model_dump(mode="json", exclude={"expected_version", "key"})),
    }
    with pytest.raises(state.StateError, match="scheduled_context_requires_setup"):
        await seed_config(
            db,
            actor.tenant_id,
            actor,
            schedule_enabled=True,
            mapping_json={**config_input().mapping_json, "scheduled_context": selection},
        )
    config, _ = await selected_config(db, actor)
    await state.control_config(
        db, actor.tenant_id, config.id, ConfigControl(enabled=False, schedule_enabled=False), actor=actor
    )
    readiness = await scheduled_detection.context_readiness(db, actor.tenant_id, config)
    assert readiness["status"] == "scope_paused" and not readiness["renewal_needed"]
