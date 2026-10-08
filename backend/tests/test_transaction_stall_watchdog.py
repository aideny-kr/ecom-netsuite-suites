"""Fault/stop/resume checks for daily collection, committed progress and alerts."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.models.audit import AuditEvent
from app.models.transaction_ops import TransactionRun
from app.schemas.transaction_runs import ProgressUpdate, RunCreate
from app.services.transaction_ops import continuation, progress_clock, watchdog
from app.services.transaction_ops import state_service as state
from tests.conftest import enable_feature_flag
from tests.test_transaction_freshness import check, entity
from tests.test_transaction_ops_state_db import seed_config

NOW = datetime(2026, 10, 8, 2, tzinfo=timezone.utc)


def deadline_run(**changes):
    return SimpleNamespace(
        **dict(
            id=uuid4(),
            origin="schedule",
            status="finished",
            termination_reason="budget",
            created_at=NOW - timedelta(minutes=15),
            finished_at=NOW,
            deadline_at=NOW,
            params_json={},
            config_snapshot={"mapping_json": {"action_mode": "propose_actions"}},
            progress_json={
                "processed": 7,
                "scan_count": 10,
                "continuation_baseline": {"processed": 7, "scan_count": 10},
            },
        )
        | changes
    )


def test_idle_deadline_earns_one_delayed_retry_then_finite_hard_stop():
    run = deadline_run()
    assert continuation.no_progress_deadline_stop(run)
    with pytest.raises(ValueError, match="read_retry_wait"):
        continuation.next_metadata(run, NOW)
    metadata = continuation.next_metadata(run, NOW + timedelta(minutes=5))
    assert metadata["continuation_no_progress_retry_count"] == 1
    assert metadata["continuation_read_retry_count"] == 1
    run.progress_json.update(metadata)
    with pytest.raises(ValueError, match="no_progress_retry_limit"):
        continuation.next_metadata(run, NOW + timedelta(minutes=30))


@pytest.mark.parametrize(
    "changes",
    [
        {"origin": "manual"},
        {"origin": "chat"},
        {"origin": "recovery"},
        {"termination_reason": "error"},
        {"finished_at": NOW - timedelta(seconds=1)},
        {"params_json": {"operation_id": str(uuid4())}},
        {"params_json": {"review": {"id": "x"}}},
        {"config_snapshot": {"mapping_json": {"action_mode": "execute"}}},
    ],
)
def test_idle_retry_excludes_early_budgets_manual_financial_and_review_work(changes):
    assert not continuation.no_progress_deadline_stop(deadline_run(**changes))


@pytest.mark.parametrize(
    "code", ["netsuite_upstream_http_401", "unclassified_read_failure", "netsuite_upstream_http_403"]
)
def test_auth_and_permanent_owned_read_diagnostics_never_earn_idle_retry(code):
    run = deadline_run()
    run.progress_json.update(
        read_stop_run_id=str(run.id),
        last_read_failure={
            "code": code,
            "resolved": False,
            "retryable": False,
            "observed_at": NOW.isoformat(),
        },
    )
    assert not continuation.no_progress_deadline_stop(run)


@pytest.mark.parametrize("counter", progress_clock.COUNTERS)
def test_productive_parts_keep_immediate_continuation_without_consuming_idle_allowance(counter):
    run = deadline_run()
    run.progress_json[counter] = run.progress_json.get(counter, 0) + 1
    assert not continuation.no_progress_deadline_stop(run)
    assert continuation.next_metadata(run, NOW)["continuation_no_progress_retry_count"] == 0


def test_progress_clock_ignores_heartbeats_reservations_and_caller_clock_spoofing():
    before = {"processed": 2, "last_progress_at": NOW.isoformat(), "execution_started_at": NOW.isoformat()}
    after = progress_clock.committed_progress(
        before,
        {**before, "orders_used": 99, "last_progress_at": (NOW + timedelta(hours=1)).isoformat()},
        NOW + timedelta(minutes=5),
    )
    assert after["last_progress_at"] == before["last_progress_at"]
    assert after["execution_started_at"] == before["execution_started_at"]
    assert (
        progress_clock.committed_progress(before, {"processed": 3}, NOW + timedelta(minutes=1))["last_progress_at"]
        != before["last_progress_at"]
    )


@pytest.mark.parametrize("phase", ["refunds", "destination", "done"])
def test_phase_advancement_counts_as_committed_progress(phase):
    assert (
        progress_clock.committed_progress({"phase": "orders"}, {"phase": phase}, NOW)["last_progress_at"]
        == NOW.isoformat()
    )


def test_inapp_stagnation_uses_commit_clock_even_with_current_heartbeat():
    e = entity()
    e["active_runs"] = [
        {
            "origin": "schedule",
            "status": "running",
            "last_progress_at": (NOW - timedelta(minutes=11)).isoformat(),
            "run_state_updated_at": NOW.isoformat(),
        }
    ]
    result = check(e, NOW)
    assert result["state"] == "alert" and result["detail"] == "progress_stalled"
    e["active_runs"][0]["last_progress_at"] = NOW.isoformat()
    assert check(e, NOW).get("detail") is None
    e["monitor"] = {"collector_stale": True}
    assert check(e, NOW)["detail"] == "collector_heartbeat_missing"


def test_queue_wait_and_first_page_timer_never_use_reserved_orders_as_progress():
    assert (
        progress_clock.stalled_snapshot(
            {"origin": "schedule", "status": "pending", "created_at": (NOW - timedelta(minutes=16)).isoformat()}, NOW
        )
        == "queue_delayed"
    )
    assert (
        progress_clock.stalled_snapshot(
            {
                "origin": "schedule",
                "status": "running",
                "execution_started_at": (NOW - timedelta(minutes=11)).isoformat(),
            },
            NOW,
        )
        == "progress_stalled"
    )
    assert (
        progress_clock.stalled_snapshot(
            {"origin": "schedule", "status": "running", "collection_wait": {"owner": "x"}}, NOW
        )
        is None
    )


@pytest.fixture
async def scheduled(db, admin_user):
    user = admin_user[0]
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, user.tenant_id, flag)
    conf = await seed_config(db, user.tenant_id, user)
    conf.schedule_enabled = True
    await db.flush()
    run = await state.create_run(
        db,
        user.tenant_id,
        conf.id,
        RunCreate(origin="schedule", evaluation_key=str(uuid4()), order_references=["R123456789"]),
        actor=user,
    )
    token = await state.claim_run(db, user.tenant_id, run.id)
    return user, conf, run, token


async def test_atomic_progress_update_preserves_owned_clocks_on_noop_and_rejects_stale_lease(db, scheduled):
    user, _, run, token = scheduled
    now = datetime.now(timezone.utc)
    updated = await state.update_progress(
        db,
        user.tenant_id,
        run.id,
        ProgressUpdate(progress_json={"processed": 1, "phase": "orders", "last_progress_at": "spoof"}),
        lease_token=token,
        now=now,
    )
    assert updated.progress_json["last_progress_at"] == now.isoformat()
    started = updated.progress_json["execution_started_at"]
    later = now + timedelta(minutes=1)
    updated = await state.update_progress(
        db,
        user.tenant_id,
        run.id,
        ProgressUpdate(progress_json={"processed": 1, "phase": "orders", "execution_started_at": "spoof"}),
        lease_token=token,
        now=later,
    )
    assert updated.progress_json["last_progress_at"] == now.isoformat()
    assert updated.progress_json["execution_started_at"] == started
    with pytest.raises(state.StateError, match="run_lease_lost"):
        await state.update_progress(
            db, user.tenant_id, run.id, ProgressUpdate(progress_json={"processed": 2}), lease_token=uuid4(), now=later
        )
    assert (await state.get_run(db, user.tenant_id, run.id)).progress_json["processed"] == 1


async def test_first_page_deadline_recovers_same_checkpoint_once_and_deduplicates(db, scheduled):
    user, _, run, token = scheduled
    finished = run.deadline_at + timedelta(seconds=1)
    parent = await state.finish_run(db, user.tenant_id, run.id, "budget", lease_token=token, now=finished)
    assert await continuation.continue_budget_run(db, user.tenant_id, parent.id, now=finished) is None
    child = await continuation.continue_budget_run(db, user.tenant_id, parent.id, now=finished + timedelta(minutes=5))
    assert child and child.params_json["order_references"] == parent.params_json["order_references"]
    assert child.progress_json["continuation_no_progress_retry_count"] == 1
    assert child.progress_json["execution_started_at"] == parent.progress_json["execution_started_at"]
    again = await continuation.continue_budget_run(db, user.tenant_id, parent.id, now=finished + timedelta(minutes=6))
    assert again.id == child.id
    assert await db.scalar(select(TransactionRun.id).where(TransactionRun.id == child.id)) == child.id


async def test_alert_partial_delivery_retries_same_payload_and_provider_key_without_duplicates(
    db, admin_user, monkeypatch
):
    tenant = admin_user[0].tenant_id
    monkeypatch.setattr(watchdog, "admin_emails", AsyncMock(return_value=["a@example.invalid", "b@example.invalid"]))
    sender = AsyncMock(side_effect=[None, RuntimeError("secret provider token"), None])
    items = [{"config_id": str(uuid4()), "name": "Inc", "reason": "progress_stalled"}]
    now = datetime.now(timezone.utc)
    assert await watchdog.notify(db, tenant, items, now=now, sender=sender) == 1
    assert await watchdog.notify(db, tenant, items, now=now + timedelta(minutes=1), sender=sender) == 0
    assert await watchdog.notify(db, tenant, items, now=now + timedelta(minutes=6), sender=sender) == 1
    assert sender.await_count == 3
    assert sender.await_args_list[1].kwargs == sender.await_args_list[2].kwargs
    assert await watchdog.notify(db, tenant, items, now=now + timedelta(minutes=7), sender=sender) == 0
    failed = await db.scalar(
        select(AuditEvent.payload).where(
            AuditEvent.tenant_id == tenant, AuditEvent.action == "recon.watchdog.alert_failed"
        )
    )
    assert failed == {"code": "delivery_failed"}


async def test_beat_outage_falls_back_only_to_scheduled_collection_and_alerts(db, admin_user, monkeypatch):
    tenant = admin_user[0].tenant_id
    monkeypatch.setattr(watchdog.feature_flag_service, "list_tenants_with_flags", AsyncMock(return_value=[tenant]))
    collector = AsyncMock(return_value={"dispatched": 1})
    monkeypatch.setattr(watchdog, "collect_due_runs", collector)
    monkeypatch.setattr(watchdog, "operational_status", AsyncMock(return_value={"entities": [], "truncated": False}))
    result = await watchdog.supervise(db, now=datetime.now(timezone.utc))
    assert result["collector_stale"] is True and result["tenants"] == 1
    assert collector.await_args.kwargs == {"scheduled_only": True}
    health = await db.scalar(
        select(AuditEvent.payload).where(AuditEvent.tenant_id == tenant, AuditEvent.action == "recon.watchdog.health")
    )
    assert health == {"collector_stale": True}


async def test_two_api_processes_cannot_supervise_together_and_shutdown_releases_lock(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def supervise(*args, **kwargs):
        calls.append(1)
        entered.set()
        await release.wait()

    monkeypatch.setattr(watchdog, "supervise", supervise)
    first = asyncio.create_task(watchdog.run_pass())
    await asyncio.wait_for(entered.wait(), timeout=5)
    await watchdog.run_pass()
    assert len(calls) == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    await watchdog.run_pass()
    assert len(calls) == 2


async def test_legacy_no_progress_audit_is_reconsidered_only_for_eligible_deadline(db, scheduled):
    user, _, run, token = scheduled
    finished = run.deadline_at + timedelta(seconds=1)
    await state.finish_run(db, user.tenant_id, run.id, "budget", lease_token=token, now=finished)
    await state._audit(db, user.tenant_id, "run.continuation_blocked", run, payload={"reason": "no_progress"})
    await db.commit()
    child = await continuation.continue_budget_run(db, user.tenant_id, run.id, now=finished + timedelta(minutes=5))
    assert child is not None


async def test_database_refuses_scope_mutation_on_scheduled_checkpoint(db, scheduled):
    _, conf, _, _ = scheduled
    conf.subsidiary_id = "999"
    with pytest.raises(DBAPIError, match="immutable transaction evidence"):
        await db.flush()
    await db.rollback()


async def test_new_daily_cycle_resets_owned_clocks_but_keeps_saved_evidence(db, scheduled):
    user, conf, run, token = scheduled
    await state.update_progress(
        db, user.tenant_id, run.id, ProgressUpdate(progress_json={"processed": 2}), lease_token=token
    )
    await state.finish_run(db, user.tenant_id, run.id, "budget", lease_token=token)
    request = RunCreate(**{**run.params_json, "evaluation_key": "schedule:next-day"})
    child = await state.create_run(db, user.tenant_id, conf.id, request, actor=None, resume_from_run_id=run.id)
    assert child.progress_json["processed"] == 2
    assert "last_progress_at" not in child.progress_json and "execution_started_at" not in child.progress_json


async def test_fallback_recovery_sql_excludes_financial_and_manual_work(db, scheduled):
    from copy import deepcopy

    from app.services.transaction_ops import scheduler

    user, conf, run, token = scheduled
    await state.finish_run(db, user.tenant_id, run.id, "budget", lease_token=token)
    other = await state.create_run(
        db,
        user.tenant_id,
        conf.id,
        RunCreate(origin="manual", evaluation_key="manual", order_references=["R123456789"]),
        actor=user,
    )
    # A synthetic recovery row proves the filter at the query boundary, without
    # creating/approving any financial operation.
    financial = TransactionRun(
        tenant_id=user.tenant_id,
        config_id=conf.id,
        origin="recovery",
        work_key=uuid4().hex,
        params_json={"operation_id": str(uuid4())},
        config_snapshot=deepcopy(run.config_snapshot),
        status="pending",
        max_api_calls=1,
        max_orders=1,
        deadline_at=run.deadline_at,
        progress_json={},
    )
    db.add(financial)
    await db.flush()
    ids = await scheduler._recovery_ids(db, user.tenant_id, datetime.now(timezone.utc), scheduled_only=True)
    assert other.id not in ids and financial.id not in ids


async def test_fresh_collector_heartbeat_prevents_fallback(db, monkeypatch):
    from app.models.job import Job

    now = datetime.now(timezone.utc)
    db.add(
        Job(
            tenant_id=watchdog.SYSTEM_TENANT,
            job_type="tasks.transaction_ops_collect_due",
            status="completed",
            completed_at=now,
            result_summary={},
        )
    )
    await db.flush()
    collector = AsyncMock()
    monkeypatch.setattr(watchdog, "collect_due_runs", collector)
    monkeypatch.setattr(watchdog.feature_flag_service, "list_tenants_with_flags", AsyncMock(return_value=[]))
    result = await watchdog.supervise(db, now=now)
    assert result["collector_stale"] is False
    collector.assert_not_awaited()


async def test_resend_sends_stable_idempotency_header_and_hides_provider_error(monkeypatch):
    import httpx

    from app.services import email_service

    calls = []

    async def post(self, *args, **kwargs):
        calls.append(kwargs)
        return httpx.Response(503, text="secret provider body")

    monkeypatch.setattr(email_service, "EMAIL_API_KEY", "private-test-key")
    monkeypatch.setattr(email_service, "EMAIL_PROVIDER", "resend")
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(RuntimeError) as failure:
        await email_service.send_recon_alert_email(
            to_email="a@example.invalid", subject="x", text_body="y", html_body="y", idempotency_key="stable-key"
        )
    assert str(failure.value) == "Resend API error: 503"
    assert calls[0]["headers"]["Idempotency-Key"] == "stable-key"
