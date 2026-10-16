"""Real scheduler/state regressions for verified Metabase reconnect recovery."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.mcp_connector import McpConnector
from app.models.transaction_ops import TransactionRun
from app.schemas.transaction_runs import ConfigControl, RunCreate
from app.services.transaction_ops import replica_recovery as recovery
from app.services.transaction_ops import scheduler
from app.services.transaction_ops import state_service as state
from tests.conftest import enable_feature_flag
from tests.test_concurrent_source_pipeline import committed as committed_fixture
from tests.test_metabase_replica_reader import BINDING
from tests.test_transaction_ops_state_db import seed_config


async def stopped(db, actor, monkeypatch, *, code="replica_transport_failed", age_hours=4):
    connector = McpConnector(
        tenant_id=actor.tenant_id,
        provider="custom",
        label="Metabase",
        server_url=BINDING["server_url"],
        auth_type="oauth2",
        status="active",
        is_enabled=True,
        encrypted_credentials="not-read-by-scheduler",
    )
    db.add(connector)
    await db.flush()
    binding = {**BINDING, "connector_id": str(connector.id)}
    config = await seed_config(
        db,
        actor.tenant_id,
        actor,
        interval_minutes=1440,
        mapping_json={
            "reference_field": "tranid",
            "action_mode": "propose_actions",
            "metabase_replica": binding,
            "reconciliation_policy": {"daily_check_hour": 9},
        },
    )
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
    await state.control_config(
        db, actor.tenant_id, config.id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
    )
    now = datetime.now(timezone.utc) + timedelta(minutes=10)
    key = scheduler._schedule_key(config, now)
    request = RunCreate(
        origin="schedule",
        evaluation_key=key,
        window_start=now - timedelta(days=4),
        window_end=now - timedelta(days=3),
    )
    prior = TransactionRun(
        id=uuid4(),
        tenant_id=actor.tenant_id,
        config_id=config.id,
        work_key=uuid4().hex,
        origin="schedule",
        params_json=request.model_dump(mode="json", exclude={"evidence_mode", "review", "window_basis"}),
        config_snapshot=state._config_snapshot(config),
        created_at=now - timedelta(hours=age_hours),
        status="finished",
        termination_reason="budget" if code == "replica_transport_failed" else "error",
        finished_at=now - timedelta(minutes=5),
        deadline_at=now - timedelta(minutes=10),
        max_api_calls=config.max_api_calls,
        max_orders=config.max_orders,
    )
    db.add(prior)
    prior.api_calls_used = 6
    prior.progress_json = {
        "schedule_cycle_key": key,
        "continuation_part": 96,
        "continuation_root_id": str(prior.id),
        "continuation_started_at": prior.created_at.isoformat(),
        "continuation_read_retry_count": 3,
        "read_retry_count": 3,
        "continuation_baseline": {"processed": 1701, "scan_count": 1802},
        "processed": 1701,
        "scan_count": 1802,
        "last_source_id": 16222670,
        "pending_refs": ["R123456789"],
        "phase": "orders",
        "refund_after_id": 123,
        "dependency_scan": {"version": 1, "stream_index": 4, "after": ["100"]},
        "evidence_root_id": str(prior.id),
        "read_stop_reason": "retry_limit",
        "read_stop_run_id": str(prior.id),
        "last_read_failure": {
            "code": code,
            "resolved": False,
            "retryable": code == "replica_transport_failed",
            "stage": "source_page",
            "run_id": str(prior.id),
            "observed_at": (now - timedelta(minutes=6)).isoformat(),
        },
    }
    connector.metadata_json = {
        "transaction_replica": binding,
        recovery.VERIFICATION_KEY: {
            "schema_version": 1,
            "id": str(uuid4()),
            "verified_at": (now - timedelta(minutes=1)).isoformat(),
            "binding": binding,
        },
    }
    await db.flush()
    monkeypatch.setattr(scheduler, "_reserve_publication", Mock(return_value=True))
    monkeypatch.setattr(scheduler, "_refresh_sources", AsyncMock(return_value=0))
    monkeypatch.setattr(scheduler.celery_app, "send_task", Mock())
    return config, prior, connector, now


@pytest.mark.parametrize(
    "code", ["replica_transport_failed", "replica_authentication_required", "replica_contract_changed"]
)
@pytest.mark.parametrize("age_hours", [4, 48])
async def test_real_scheduler_resumes_verified_reconnect_without_waiting_or_restarting(
    db, admin_user, monkeypatch, code, age_hours
):
    actor = admin_user[0]
    config, prior, _, now = await stopped(db, actor, monkeypatch, code=code, age_hours=age_hours)
    saved = deepcopy(prior.progress_json)
    assert await recovery.recovery_ready(db, actor.tenant_id, prior, config, now) is not None
    assert config.id in await scheduler._candidate_ids(db, actor.tenant_id, now)
    stats = await scheduler.collect_due_runs(db, now)
    assert stats["created"] == 1, stats
    child = await db.scalar(
        select(TransactionRun).where(TransactionRun.config_id == config.id, TransactionRun.id != prior.id)
    )
    assert child.status == "pending" and child.origin == "schedule"
    assert child.params_json["window_start"] == prior.params_json["window_start"]
    assert child.params_json["window_end"] == prior.params_json["window_end"]
    for field in (
        "last_source_id",
        "pending_refs",
        "processed",
        "scan_count",
        "refund_after_id",
        "dependency_scan",
        "evidence_root_id",
        "last_read_failure",
    ):
        assert child.progress_json[field] == saved[field]
    assert child.progress_json["last_read_failure"]["resolved"] is False
    assert child.progress_json["schedule_cycle_key"] == scheduler._schedule_key(config, now)
    assert child.progress_json["replica_recovery"]["previous_read_retries_used"] == 3
    assert child.api_calls_used == 0 and child.max_api_calls == config.max_api_calls
    assert child.deadline_at == now + timedelta(seconds=config.deadline_seconds)
    assert prior.progress_json == saved and prior.api_calls_used == 6
    assert (await scheduler.collect_due_runs(db, now))["created"] == 0
    audit = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_id == str(child.id),
            AuditEvent.action == "transaction_ops.run.replica_recovery_authorized",
        )
    )
    assert audit.payload["parent_run_id"] == str(prior.id)
    assert audit.payload["new_finite_cycle"] is True
    # Lost-message recovery may republish the same pending run. There is one
    # durable owner, so repeated deliveries cannot spend twice.
    assert {call.kwargs["kwargs"]["run_id"] for call in scheduler.celery_app.send_task.call_args_list} == {
        str(child.id)
    }


@pytest.mark.parametrize(
    "guard",
    [
        "no_proof",
        "old_proof",
        "expired_proof",
        "future_proof",
        "invalid_id",
        "wrong_binding",
        "revoked",
        "disabled_connector",
        "disabled_schedule",
        "disabled_config",
        "scope_changed",
        "action_mode",
        "reused_cycle",
        "resolved",
        "wrong_owner",
        "inherited_failure",
        "foreign_tenant",
        "manual",
        "unsafe_code",
        "feature_disabled",
        "restart_scan",
        "saved_evidence",
    ],
)
async def test_stored_recovery_guards_fail_closed(db, admin_user, tenant_b, monkeypatch, guard):
    actor = admin_user[0]
    config, prior, connector, now = await stopped(db, actor, monkeypatch)
    await db.refresh(config)
    await db.refresh(prior)
    # Immutable config/run identity is protected by DB triggers too. Use
    # snapshots to exercise the eligibility checks without disabling triggers.
    config = SimpleNamespace(**{c.name: getattr(config, c.name) for c in config.__table__.columns})
    prior = SimpleNamespace(**{c.name: getattr(prior, c.name) for c in prior.__table__.columns})
    meta, progress = deepcopy(connector.metadata_json), deepcopy(prior.progress_json)
    proof = meta[recovery.VERIFICATION_KEY]
    if guard == "no_proof":
        meta.pop(recovery.VERIFICATION_KEY)
    elif guard == "old_proof":
        proof["verified_at"] = (now - timedelta(hours=1)).isoformat()
    elif guard == "expired_proof":
        prior.created_at = now - timedelta(hours=48)
        prior.finished_at = now - timedelta(hours=26)
        progress["last_read_failure"]["observed_at"] = (now - timedelta(hours=27)).isoformat()
        proof["verified_at"] = (now - timedelta(hours=25)).isoformat()
    elif guard == "future_proof":
        proof["verified_at"] = (now + timedelta(seconds=1)).isoformat()
    elif guard == "invalid_id":
        proof["id"] = "bad"
    elif guard == "wrong_binding":
        proof["binding"] = {**proof["binding"], "database_id": 999}
    elif guard == "revoked":
        connector.status = "revoked"
    elif guard == "disabled_connector":
        connector.is_enabled = False
    elif guard == "disabled_schedule":
        config.schedule_enabled = False
    elif guard == "disabled_config":
        config.enabled = False
    elif guard == "scope_changed":
        config.subsidiary_id = "2"
    elif guard == "action_mode":
        config.mapping_json = {**config.mapping_json, "action_mode": "auto_execute"}
    elif guard == "reused_cycle":
        progress["replica_recovery"] = {"cycle_key": scheduler._schedule_key(config, now)}
    elif guard == "resolved":
        progress["last_read_failure"]["resolved"] = True
    elif guard == "wrong_owner":
        progress["last_read_failure"]["run_id"] = str(uuid4())
    elif guard == "inherited_failure":
        progress["last_read_failure"]["observed_at"] = (prior.created_at - timedelta(seconds=1)).isoformat()
    elif guard == "foreign_tenant":
        connector.tenant_id = tenant_b.id
    elif guard == "manual":
        prior.origin = "manual"
    elif guard == "unsafe_code":
        progress["last_read_failure"]["code"] = "netsuite_upstream_http_401"
    elif guard == "feature_disabled":
        await enable_feature_flag(db, actor.tenant_id, "reconciliation", enabled=False)
    elif guard == "restart_scan":
        progress["restart_scan"] = True
    elif guard == "saved_evidence":
        prior.params_json = {**prior.params_json, "evidence_mode": "saved"}
    connector.metadata_json, prior.progress_json = meta, progress
    await db.flush()
    assert await recovery.recovery_ready(db, actor.tenant_id, prior, config, now) is None


async def test_a_second_successful_test_cannot_create_another_recovery_same_cycle(db, admin_user, monkeypatch):
    actor = admin_user[0]
    config, prior, connector, now = await stopped(db, actor, monkeypatch)
    proof = await recovery.recovery_ready(db, actor.tenant_id, prior, config, now)
    request = RunCreate(**{**prior.params_json, "evaluation_key": recovery.recovery_key(prior, proof)})
    child = await state.create_run(
        db, actor.tenant_id, config.id, request, now=now, resume_from_run_id=prior.id, automatic_replica_recovery=True
    )
    assert (
        await state.create_run(
            db,
            actor.tenant_id,
            config.id,
            request,
            now=now,
            resume_from_run_id=prior.id,
            automatic_replica_recovery=True,
        )
    ).id == child.id
    child.status, child.termination_reason = "finished", "budget"
    child.finished_at = now + timedelta(minutes=1)
    child.progress_json = {
        **child.progress_json,
        "read_stop_run_id": str(child.id),
        "last_read_failure": {
            **child.progress_json["last_read_failure"],
            "run_id": str(child.id),
            "observed_at": (now + timedelta(seconds=30)).isoformat(),
        },
    }
    connector.metadata_json = {
        **connector.metadata_json,
        recovery.VERIFICATION_KEY: {
            **connector.metadata_json[recovery.VERIFICATION_KEY],
            "id": str(uuid4()),
            "verified_at": (now + timedelta(minutes=2)).isoformat(),
        },
    }
    await db.flush()
    assert await recovery.recovery_ready(db, actor.tenant_id, child, config, now + timedelta(minutes=3)) is None
    with pytest.raises(state.StateError, match="invalid_run_continuation"):
        await state.create_run(
            db,
            actor.tenant_id,
            config.id,
            RunCreate(**{**prior.params_json, "evaluation_key": "forged"}),
            now=now,
            resume_from_run_id=prior.id,
            automatic_replica_recovery=True,
        )


# Use real commits to prove two collectors serialize around the shared config lock.
@pytest.fixture
async def committed(monkeypatch):
    async for value in committed_fixture.__wrapped__(monkeypatch):
        yield value


async def test_concurrent_collectors_commit_only_one_recovery(committed, monkeypatch):
    db, actor, factory = committed
    config, prior, _, now = await stopped(db, actor, monkeypatch)
    await db.commit()

    async def collect():
        async with factory() as other:
            return await scheduler.collect_due_runs(other, now)

    results = await asyncio.gather(collect(), collect())
    assert sum(result["created"] for result in results) == 1
    async with factory() as other:
        children = list(
            await other.scalars(
                select(TransactionRun).where(TransactionRun.config_id == config.id, TransactionRun.id != prior.id)
            )
        )
        assert len(children) == 1


async def test_ops_status_exposes_eligibility_without_claiming_dispatch(db, admin_user, monkeypatch):
    from app.services.transaction_ops.operational_status import operational_status

    actor = admin_user[0]
    config, _, _, now = await stopped(db, actor, monkeypatch)
    result = await operational_status(db, actor.tenant_id, config_id=config.id, now=now)
    entity = result["entities"][0]
    assert entity["active_runs"] == []
    assert entity["next_action"]["kind"] == "continue_checkpoint"
    assert entity["next_action"]["reason"] == "replica_connection_verified"
    assert entity["next_action"]["dispatch_verified"] is False


async def test_busy_connection_does_not_block_state_gate_or_other_scopes(committed, monkeypatch):
    db, actor, factory = committed
    config, prior, connector, now = await stopped(db, actor, monkeypatch)
    tenant_id, config_id, parent_id, connector_id = actor.tenant_id, config.id, prior.id, connector.id
    proof = await recovery.recovery_ready(db, tenant_id, prior, config, now)
    request = RunCreate(**{**prior.params_json, "evaluation_key": recovery.recovery_key(prior, proof)})
    await db.commit()
    async with factory() as holder:
        await holder.scalar(select(McpConnector).where(McpConnector.id == connector_id).with_for_update())
        async with factory() as reader:
            with pytest.raises(state.StateError, match="invalid_run_continuation"):
                async with asyncio.timeout(2):
                    await state.create_run(
                        reader,
                        tenant_id,
                        config_id,
                        request,
                        now=now,
                        resume_from_run_id=parent_id,
                        automatic_replica_recovery=True,
                    )
            await reader.rollback()
        # Lock remains held. The scheduler must skip it and finish the tick;
        # no blocked broker/provider operation, no recovery run was created.
        async with factory() as collector:
            async with asyncio.timeout(2):
                stats = await scheduler.collect_due_runs(collector, now)
            assert stats["created"] == 0
            assert stats["config_failed"] == 1
    async with factory() as reader:
        child = await state.create_run(
            reader,
            tenant_id,
            config_id,
            request,
            now=now,
            resume_from_run_id=parent_id,
            automatic_replica_recovery=True,
        )
        assert child.status == "pending"


async def test_state_rejects_valid_proof_for_a_superseded_parent(db, admin_user, monkeypatch):
    actor = admin_user[0]
    config, prior, _, now = await stopped(db, actor, monkeypatch)
    proof = await recovery.recovery_ready(db, actor.tenant_id, prior, config, now)
    request = RunCreate(**{**prior.params_json, "evaluation_key": recovery.recovery_key(prior, proof)})
    newer = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(**{**prior.params_json, "evaluation_key": "next-genuine-cycle"}),
        now=now,
    )
    newer.status, newer.termination_reason, newer.finished_at = "finished", "budget", now
    await db.flush()
    assert await recovery.recovery_ready(db, actor.tenant_id, prior, config, now) is not None
    with pytest.raises(state.StateError, match="invalid_run_continuation"):
        await state.create_run(
            db,
            actor.tenant_id,
            config.id,
            request,
            now=now,
            resume_from_run_id=prior.id,
            automatic_replica_recovery=True,
        )
