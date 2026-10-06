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


async def selected_config(db, actor, variant="approved", *, request=None, **config_changes):
    request = request or draft()
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
        db,
        actor.tenant_id,
        actor,
        mapping_json={**config_input().mapping_json, "scheduled_context": selection},
        **config_changes,
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


@pytest.mark.parametrize("revocation", ["visibility", "source"])
async def test_activation_rechecks_execution_authority(db, admin_user, tenant_a, revocation):
    from uuid import uuid4

    from sqlalchemy import delete

    from app.models.user import Permission, Role, RolePermission, UserRole
    from tests.conftest import create_test_user

    actor = admin_user[0]
    manager, _ = await create_test_user(db, tenant_a, role_name="admin")
    if revocation == "source":
        from tests.test_solidus_ingestion import connection

        source = await connection(db, actor.tenant_id, metadata_json={"api_profile": "framework_sync"})
        config, _ = await selected_config(db, actor, source_step_id=None, source_connection_id=source.id)
    else:
        config, _ = await selected_config(db, actor)
    if revocation == "visibility":
        await db.execute(delete(UserRole).where(UserRole.tenant_id == actor.tenant_id, UserRole.user_id == actor.id))
        role = Role(name="scheduled-readonly-" + uuid4().hex[:12])
        db.add(role)
        await db.flush()
        permission = await db.scalar(select(Permission).where(Permission.codename == "recon.run"))
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
        db.add(UserRole(tenant_id=actor.tenant_id, user_id=actor.id, role_id=role.id))
        await db.flush()
        await state._human(db, actor.tenant_id, actor, "recon.run")
    else:
        source.status = "revoked"
        await db.flush()
    readiness = await scheduled_detection.context_readiness(db, actor.tenant_id, config)
    assert readiness == {
        "ready": False,
        "status": "execution_access_unavailable",
        "review_by": None,
        "renewal_needed": False,
    }
    with pytest.raises(state.StateError, match="scheduled_detection_access_revoked"):
        await state.control_config(
            db, actor.tenant_id, config.id, ConfigControl(enabled=True, schedule_enabled=True), actor=manager
        )
    assert not (await state.get_config(db, actor.tenant_id, config.id)).schedule_enabled
    # Revocation must not stop an authorized manager from pausing the scope.
    await state.control_config(
        db, actor.tenant_id, config.id, ConfigControl(enabled=False, schedule_enabled=False), actor=manager
    )


async def test_selected_currency_conflict_cannot_clear_case(db, admin_user):
    from app.services.transaction_ops import case_service
    from tests.test_context_provenance import SCOPE
    from tests.test_scheduled_detection import NOW, evidence

    actor = admin_user[0]
    config, _ = await selected_config(
        db, actor, request=draft(scope={**SCOPE.model_dump(), "currency": "EUR"}), netsuite_account_id="1234567_SB1"
    )
    await state.control_config(
        db, actor.tenant_id, config.id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
    )
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(origin="schedule", evaluation_key="currency-conflict", order_references=["R123456789"]),
        now=NOW,
    )
    lease = await state.claim_run(db, actor.tenant_id, run.id, now=NOW)
    finding = await state.record_finding(
        db, actor.tenant_id, run.id, "R123456789", evidence(), lease_token=lease, now=NOW
    )
    receipt = finding.report_json["scheduled_detection"]
    assert receipt["outcome"] == "incomplete_evidence"
    assert receipt["accounting_context"]["status"] == "selected_currency_conflict"
    assert finding.report_json["balance"]["status"] == "incomplete"
    assert (await case_service.list_cases(db, actor.tenant_id))[0].status == "open"


async def test_selected_context_expiry_between_claim_and_spend_stalls(db, admin_user):
    actor = admin_user[0]
    config, request = await selected_config(db, actor)
    await state.control_config(
        db, actor.tenant_id, config.id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
    )
    before = request.review_by - timedelta(seconds=30)
    run = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(origin="schedule", evaluation_key="expiry-after-claim", order_references=["R123456789"]),
        now=before,
    )
    lease = await state.claim_run(db, actor.tenant_id, run.id, now=before)
    assert lease
    assert not await state.reserve_budget(
        db, actor.tenant_id, run.id, lease_token=lease, api_calls=1, now=request.review_by + timedelta(seconds=1)
    )
    current = await state.get_run(db, actor.tenant_id, run.id)
    assert current.termination_reason == "stall" and current.api_calls_used == 0
    assert current.progress_json["reason"] == "scheduled_context_requires_review"


async def test_replica_successor_requires_its_own_context_review(db, admin_user, monkeypatch):
    from app.services.transaction_ops import replica_setup
    from tests.test_metabase_replica_reader import BINDING
    from tests.test_transaction_replica_setup import connector

    actor = admin_user[0]
    old, _ = await selected_config(db, actor)
    await state.control_config(
        db, actor.tenant_id, old.id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
    )
    replica = await connector(db, actor)
    monkeypatch.setattr(
        replica_setup.metabase_reader, "read_order_page", AsyncMock(return_value={"orders": [], "page_complete": True})
    )
    new = (
        await replica_setup.bind_verified_replica(
            db, actor.tenant_id, [old.id], {**BINDING, "connector_id": str(replica.id)}, actor=actor
        )
    )[0]
    assert new.enabled and not new.schedule_enabled
    assert not old.enabled and not old.schedule_enabled
    assert not (await scheduled_detection.context_readiness(db, actor.tenant_id, new))["ready"]


async def test_invalidated_selection_cannot_sponsor_daily_source_refresh(db, admin_user, monkeypatch):
    from unittest.mock import Mock
    from app.services.ingestion import solidus_dispatch
    from tests.test_solidus_ingestion import connection

    actor = admin_user[0]
    source = await connection(db, actor.tenant_id, metadata_json={"api_profile": "framework_sync"})
    config, _ = await selected_config(db, actor, source_step_id=None, source_connection_id=source.id)
    await state.control_config(
        db, actor.tenant_id, config.id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
    )
    await context_provenance.decide_context(
        db,
        actor.tenant_id,
        config.id,
        "revenue",
        decision(await read(db, actor, config), kind="invalidate"),
        actor=actor,
    )
    publish = Mock()
    monkeypatch.setattr(solidus_dispatch, "publish_refresh", publish)
    assert await solidus_dispatch.refresh_sponsor(db, actor.tenant_id, source.id) is None
    result = await solidus_dispatch.queue_refresh(db, actor.tenant_id, source.id, daily=True)
    assert result["status"] == "unavailable"
    publish.assert_not_called()
