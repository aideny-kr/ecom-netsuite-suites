"""Actual scheduler/state recovery of a saved authentication-failure checkpoint."""

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select

from app.core.encryption import encrypt_credentials
from app.models.audit import AuditEvent
from app.models.connection import Connection
from app.models.transaction_ops import TransactionRun
from app.schemas.transaction_runs import ConfigControl, RunCreate
from app.services.transaction_ops import continuation as cont
from app.services.transaction_ops import scheduler
from app.services.transaction_ops import state_service as state
from tests.conftest import enable_feature_flag
from tests.test_concurrent_source_pipeline import committed as committed_fixture
from tests.test_transaction_ops_state_db import seed_config


async def failed_auth(db, actor, monkeypatch, *, progress_guard=None, code="netsuite_upstream_http_401"):
    config = await seed_config(db, actor.tenant_id, actor, interval_minutes=1440)
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
    await state.control_config(
        db, actor.tenant_id, config.id, ConfigControl(enabled=True, schedule_enabled=True), actor=actor
    )
    now = datetime.now(timezone.utc) + timedelta(minutes=20)
    prior = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(
            origin="schedule",
            evaluation_key=scheduler._bucket(now, 1440),
            window_start=now - timedelta(days=2),
            window_end=now - timedelta(days=1),
        ),
        now=now,
    )
    prior.status, prior.termination_reason = "finished", "error"
    prior.finished_at = now - timedelta(minutes=5)
    prior.progress_json = {
        "processed": 1801,
        "scan_count": 2452,
        "last_source_id": 16222670,
        "pending_refs": [],
        "dependency_step_count": 922,
        "dependency_scan": {"version": 1, "stream_index": 4, "after": ["100"]},
        "continuation_part": 12,
        "continuation_root_id": str(prior.id),
        "evidence_root_id": str(prior.id),
        "continuation_started_at": (now - timedelta(hours=4)).isoformat(),
        "schedule_cycle_key": scheduler._bucket(now, 1440),
        "continuation_baseline": {"processed": 1801, "scan_count": 2452, "dependency_step_count": 922},
        "last_read_failure": {
            "code": code,
            "resolved": False,
            "retryable": False,
            "stage": "dependency_owners",
            "observed_at": (now - timedelta(minutes=6)).isoformat(),
        },
    }
    if progress_guard == "exhausted":
        prior.progress_json["auth_resume_count"] = 1
    elif progress_guard == "expired_cycle":
        prior.progress_json["continuation_started_at"] = (now - timedelta(days=2)).isoformat()
    elif progress_guard == "part_limit":
        prior.progress_json["continuation_part"] = cont.SCHEDULE_MAX_PARTS
    elif progress_guard == "same_rejected_token":
        prior.progress_json["last_read_failure"].update(
            auth_token_sha256=hashlib.sha256(b"new-token").hexdigest(),
            auth_connection_id=str(config.netsuite_connection_id),
            auth_account_id=config.netsuite_account_id,
        )
    conn = await db.scalar(
        select(Connection).where(
            Connection.tenant_id == actor.tenant_id, Connection.id == config.netsuite_connection_id
        )
    )
    credentials = {
        "account_id": config.netsuite_account_id,
        "access_token": "new-token",
        "refresh_token": "refresh",
        "client_id": "client",
        "expires_at": now.timestamp() + 3360,
    }
    conn.encrypted_credentials = encrypt_credentials(credentials)
    await db.flush()
    monkeypatch.setattr(scheduler, "_reserve_publication", Mock(return_value=True))
    monkeypatch.setattr(scheduler, "_refresh_sources", AsyncMock(return_value=0))
    monkeypatch.setattr(scheduler.celery_app, "send_task", Mock())
    return config, prior, conn, credentials, now


@pytest.mark.parametrize(
    "code", ["netsuite_upstream_http_401", "netsuite_invalid_connection", "netsuite_authentication_failed"]
)
async def test_scheduler_recovers_auth_stop_once_without_restarting_window(db, admin_user, monkeypatch, code):
    actor = admin_user[0]
    config, prior, _, _, now = await failed_auth(db, actor, monkeypatch, code=code)
    saved = dict(prior.progress_json)
    assert config.id in await scheduler._candidate_ids(db, actor.tenant_id, now)
    assert (await scheduler.collect_due_runs(db, now))["created"] == 1
    child, blocked = await cont.continuation_result(db, actor.tenant_id, prior.id)
    assert blocked is None
    assert child.progress_json["auth_resume_count"] == 1
    assert child.progress_json["continuation_part"] == 13
    for key in (
        "processed",
        "scan_count",
        "last_source_id",
        "pending_refs",
        "dependency_scan",
        "evidence_root_id",
        "continuation_started_at",
    ):
        assert child.progress_json[key] == saved[key]
    assert child.params_json["window_start"] == prior.params_json["window_start"]
    assert child.params_json["window_end"] == prior.params_json["window_end"]
    assert (await cont.continue_budget_run(db, actor.tenant_id, prior.id, now=now)).id == child.id
    assert (await scheduler.collect_due_runs(db, now))["created"] == 0


@pytest.mark.parametrize(
    "code", ["netsuite_upstream_http_401", "netsuite_invalid_connection", "netsuite_authentication_failed"]
)
@pytest.mark.parametrize(
    "guard",
    [
        "old_token",
        "expired_token",
        "same_rejected_token",
        "revoked",
        "wrong_account",
        "hard_audit",
        "exhausted",
        "expired_cycle",
    ],
)
async def test_auth_recovery_guards_never_create_a_child(db, admin_user, monkeypatch, guard, code):
    actor = admin_user[0]
    config, prior, conn, credentials, now = await failed_auth(db, actor, monkeypatch, progress_guard=guard, code=code)
    if guard == "old_token":
        credentials["expires_at"] = now.timestamp() + 3000  # issued ten minutes ago, before failure
    elif guard == "expired_token":
        credentials["expires_at"] = now.timestamp() + 10
    elif guard == "revoked":
        conn.status = "revoked"
    elif guard == "wrong_account":
        credentials["account_id"] = "999999"
    elif guard == "hard_audit":
        await state._audit(
            db, actor.tenant_id, "run.continuation_blocked", prior, payload={"reason": "permission_denied"}
        )
    conn.encrypted_credentials = encrypt_credentials(credentials)
    await db.flush()
    for n in range(3):
        assert await cont.continue_budget_run(db, actor.tenant_id, prior.id, now=now + timedelta(seconds=n)) is None
    assert (
        await db.scalar(
            select(func.count())
            .select_from(TransactionRun)
            .where(TransactionRun.tenant_id == actor.tenant_id, TransactionRun.config_id == config.id)
        )
        == 1
    )
    audits = await db.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.tenant_id == actor.tenant_id,
            AuditEvent.resource_id == str(prior.id),
            AuditEvent.action == "transaction_ops.run.continuation_blocked",
        )
    )
    assert audits == int(guard in ("hard_audit", "exhausted", "expired_cycle"))


async def test_next_daily_cutoff_does_not_discard_stranded_checkpoint(db, admin_user, monkeypatch):
    actor = admin_user[0]
    _, prior, conn, credentials, now = await failed_auth(db, actor, monkeypatch)
    tomorrow = now + timedelta(days=1)
    credentials["expires_at"] = tomorrow.timestamp() + 3500
    conn.encrypted_credentials = encrypt_credentials(credentials)
    await db.flush()
    assert (await scheduler.collect_due_runs(db, tomorrow))[
        "created"
    ] == 0  # finite cycle age, explicit stop, no restart
    assert (await state.get_run(db, actor.tenant_id, prior.id)).progress_json["processed"] == 1801


async def test_second_401_after_recovery_cannot_reset_allowance_at_cutoff(db, admin_user, monkeypatch):
    actor = admin_user[0]
    _, prior, conn, credentials, now = await failed_auth(db, actor, monkeypatch)
    child = await cont.continue_budget_run(db, actor.tenant_id, prior.id, now=now)
    child.status, child.termination_reason = "finished", "error"
    child.finished_at = now + timedelta(minutes=1)
    child.progress_json = {
        **child.progress_json,
        "last_read_failure": {
            "code": "netsuite_upstream_http_401",
            "resolved": False,
            "observed_at": (now + timedelta(seconds=30)).isoformat(),
        },
    }
    credentials["expires_at"] = (now + timedelta(days=1, minutes=59)).timestamp()
    conn.encrypted_credentials = encrypt_credentials(credentials)
    await db.flush()
    assert (await scheduler.collect_due_runs(db, now + timedelta(days=1)))["created"] == 0
    assert (await state.get_run(db, actor.tenant_id, child.id)).progress_json["auth_resume_count"] == 1


@pytest.mark.parametrize("code", ["netsuite_invalid_connection", "netsuite_authentication_failed"])
@pytest.mark.parametrize("limit", ["expired_cycle", "exhausted", "part_limit"])
async def test_pre_http_stop_keeps_next_daily_cycle_after_resume_limit(db, admin_user, monkeypatch, code, limit):
    actor = admin_user[0]
    config, prior, _, _, now = await failed_auth(db, actor, monkeypatch, code=code, progress_guard=limit)
    assert (await scheduler.collect_due_runs(db, now))["created"] == 0
    assert (await scheduler.collect_due_runs(db, now + timedelta(days=1)))["created"] == 1
    _, latest = await scheduler._schedule_history(db, actor.tenant_id, config.id)
    assert latest.id != prior.id and latest.status == "pending"
    assert latest.params_json["window_start"] == prior.params_json["window_start"]
    assert latest.params_json["window_end"] == prior.params_json["window_end"]
    assert "continuation_of" not in latest.progress_json
    assert (await scheduler.collect_due_runs(db, now + timedelta(days=1, minutes=1)))["created"] == 0


@pytest.fixture
async def committed(monkeypatch):
    async for value in committed_fixture.__wrapped__(monkeypatch):
        yield value


@pytest.mark.parametrize("code", ["netsuite_upstream_http_401", "netsuite_invalid_connection"])
async def test_concurrent_real_transactions_create_one_auth_resume(committed, monkeypatch, code):
    db, actor, factory = committed
    _, prior, _, _, now = await failed_auth(db, actor, monkeypatch, code=code)
    prior_id = prior.id
    await db.commit()

    async def resume():
        async with factory() as session:
            return (await cont.continue_budget_run(session, actor.tenant_id, prior_id, now=now)).id

    ids = await asyncio.gather(resume(), resume())
    assert ids[0] == ids[1]
    assert (
        await db.scalar(
            select(func.count())
            .select_from(TransactionRun)
            .where(
                TransactionRun.tenant_id == actor.tenant_id,
                TransactionRun.progress_json["continuation_of"].astext == str(prior_id),
            )
        )
        == 1
    )


async def test_resume_refuses_changed_config_or_foreign_tenant(db, admin_user, monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    from app.services.transaction_ops.auth_recovery import auth_resume_ready

    actor = admin_user[0]
    config, prior, _, _, now = await failed_auth(db, actor, monkeypatch)
    altered = SimpleNamespace(
        netsuite_connection_id=config.netsuite_connection_id,
        netsuite_account_id=config.netsuite_account_id,
        subsidiary_id="different",
    )
    assert not await auth_resume_ready(db, actor.tenant_id, prior, altered, now)
    assert not await auth_resume_ready(db, uuid4(), prior, config, now)


async def test_reconnection_wakes_checkpoint_before_next_daily_cutoff(db, admin_user, monkeypatch):
    actor = admin_user[0]
    _, prior, conn, credentials, now = await failed_auth(db, actor, monkeypatch, code="netsuite_invalid_connection")
    saved = dict(prior.progress_json)
    conn.status = "error"
    await db.flush()
    assert (await scheduler.collect_due_runs(db, now))["created"] == 0
    assert (await cont.continuation_result(db, actor.tenant_id, prior.id))[0] is None
    scheduler.celery_app.send_task.assert_not_called()

    # Reconnection has committed a newer token on the same connection. Beat's
    # existing minute tick must wake the saved read, not await tomorrow's key.
    conn.status = "active"
    conn.encrypted_credentials = encrypt_credentials({**credentials, "issued_at": now.timestamp() + 1})
    await db.flush()
    tick = now + timedelta(seconds=2)
    assert (await scheduler.collect_due_runs(db, tick))["created"] == 1
    child, blocked = await cont.continuation_result(db, actor.tenant_id, prior.id)
    assert blocked is None and child.status == "pending"
    for key in ("last_source_id", "processed", "dependency_scan", "evidence_root_id"):
        assert child.progress_json[key] == saved[key]
    assert child.progress_json["auth_resume_count"] == 1
    assert child.progress_json["last_read_failure"]["resolved"] is False
    assert child.params_json["window_start"] == prior.params_json["window_start"]
    assert child.params_json["window_end"] == prior.params_json["window_end"]
    assert scheduler.celery_app.send_task.call_args.kwargs["queue"] == scheduler.DAILY_QUEUE
    assert (await scheduler.collect_due_runs(db, tick + timedelta(seconds=60)))["created"] == 0


@pytest.mark.parametrize(
    "code", ["netsuite_account_mismatch", "netsuite_invalid_connection_account", "netsuite_upstream_http_403"]
)
async def test_reconnection_does_not_retry_scope_or_permission_failures(db, admin_user, monkeypatch, code):
    from app.services.transaction_ops.auth_recovery import auth_stop

    actor = admin_user[0]
    _, prior, _, _, now = await failed_auth(db, actor, monkeypatch, code=code)
    assert not auth_stop(prior)
    assert await cont.continue_budget_run(db, actor.tenant_id, prior.id, now=now) is None
    assert (await scheduler.collect_due_runs(db, now))["created"] == 0


async def test_real_reader_stop_reconnect_scheduler_and_runner_complete(db, admin_user, monkeypatch):
    from app.services.transaction_ops import netsuite_reader, staged_netsuite
    from app.services.transaction_ops.runner import run_investigation
    from tests.test_transaction_chunk_runner import setup

    actor = admin_user[0]
    prior, refs, _, _, _ = await setup(db, actor, monkeypatch, origin="schedule")
    tenant_id, prior_id = actor.tenant_id, prior.id
    config = await state.get_config(db, tenant_id, prior.config_id)
    connection_id, account_id = config.netsuite_connection_id, config.netsuite_account_id
    prior_window_start = prior.params_json["window_start"]
    conn = await db.get(Connection, connection_id)
    conn.status = "error"
    conn.encrypted_credentials = encrypt_credentials({"account_id": account_id})
    await db.commit()
    base = staged_netsuite.StagedNetSuite

    class AuthenticatedTarget(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.read = kwargs["read"]

        async def order(self, reference, **kwargs):
            # Exercise the real pre-HTTP connection gate, then fixture evidence;
            # no provider calls or financial writes are permitted by this test.
            async def evidence():
                async with netsuite_reader.authenticated_reader(db, tenant_id, connection_id, account_id):
                    return await super(AuthenticatedTarget, self).order(reference, **kwargs)

            return await self.read("netsuite_order", evidence, held=0, data_calls=0)

    monkeypatch.setattr(staged_netsuite, "StagedNetSuite", AuthenticatedTarget)
    token = AsyncMock(return_value="fresh-access")
    monkeypatch.setattr(netsuite_reader, "get_valid_token", token)
    monkeypatch.setattr(scheduler, "_reserve_publication", Mock(return_value=True))
    monkeypatch.setattr(scheduler, "_refresh_sources", AsyncMock(return_value=0))
    monkeypatch.setattr(scheduler.celery_app, "send_task", Mock())
    assert (await run_investigation(db, tenant_id, prior_id))["termination_reason"] == "error"
    token.assert_not_called()
    failed = await state.get_run(db, tenant_id, prior_id)
    assert failed.progress_json["last_read_failure"]["code"] == "netsuite_invalid_connection"
    assert failed.progress_json["pending_refs"] == refs
    assert (await scheduler.collect_due_runs(db, await state.run_clock(db)))["created"] == 0

    now = await state.run_clock(db)
    conn = await db.get(Connection, connection_id, populate_existing=True)
    conn.status = "active"
    conn.encrypted_credentials = encrypt_credentials(
        {
            "account_id": account_id,
            "access_token": "fresh-access",
            "issued_at": now.timestamp(),
            "expires_at": now.timestamp() + 3600,
        }
    )
    await db.commit()
    assert (await scheduler.collect_due_runs(db, await state.run_clock(db)))["created"] == 1
    child, _ = await cont.continuation_result(db, tenant_id, prior_id)
    assert child.progress_json["pending_refs"] == refs
    assert child.params_json["window_start"] == prior_window_start
    result = await run_investigation(db, tenant_id, child.id)
    assert result["termination_reason"] == "done" and result["processed"] == len(refs)
    child = await state.get_run(db, tenant_id, child.id)
    assert child.progress_json["last_read_failure"]["resolved"] is True
    assert child.progress_json["pending_refs"] == []
    assert len(await state.list_findings(db, tenant_id, child.id)) == len(refs)
    assert not await state.list_proposals(db, tenant_id, run_id=child.id)
