"""Operational answers use persisted evidence, not provider reads or healthy-process guesses."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.models.transaction_ops import TransactionRun
from app.services.transaction_ops import operational_status as status
from app.services.transaction_ops import state_service
from tests.conftest import enable_feature_flag
from tests.test_transaction_ops_state_db import seed_config

NOW = datetime(2026, 9, 28, 15, tzinfo=timezone.utc)  # 08:00 PDT, before daily cutoff


def config(**changes):
    return SimpleNamespace(
        **dict(
            id=uuid4(),
            enabled=True,
            schedule_enabled=True,
            interval_minutes=60,
            mapping_json={"reconciliation_policy": {"timezone_name": "America/Los_Angeles"}},
        )
        | changes
    )


def run(**changes):
    return SimpleNamespace(
        **dict(
            id=uuid4(),
            origin="schedule",
            status="finished",
            termination_reason="budget",
            created_at=NOW - timedelta(minutes=20),
            finished_at=NOW - timedelta(minutes=2),
            updated_at=NOW,
            deadline_at=NOW + timedelta(minutes=10),
            lease_until=NOW + timedelta(minutes=1),
            progress_json={"processed": 10},
            params_json={},
            max_api_calls=100,
            api_calls_used=12,
            api_calls_held=0,
            max_orders=50,
            orders_used=10,
        )
        | changes
    )


def test_schedule_cutoff_and_dst_are_calendar_based():
    assert status.schedule(config(), NOW)["next_check_at"] == "2026-09-28T16:00:00+00:00"
    at_cutoff = status.schedule(config(), NOW + timedelta(hours=1))
    assert at_cutoff["next_check_at"] == "2026-09-29T16:00:00+00:00"
    winter = datetime(2026, 11, 1, 16, tzinfo=timezone.utc)
    assert status.schedule(config(), winter)["next_check_at"] == "2026-11-01T17:00:00+00:00"
    assert status.schedule(config(schedule_enabled=False), NOW)["next_check_at"] is None


def test_progress_does_not_invent_a_rate_or_last_advance_time():
    row = run(
        progress_json={
            "phase": "refunds",
            "processed": 10,
            "matched": 8,
            "needs_review": 2,
            "pending_refs": ["private-order"],
            "secret": "do-not-return",
        }
    )
    result = status.run_snapshot(row, NOW)
    assert result["run_state_updated_at"] == NOW.isoformat()
    assert result["last_progress_at"] is None
    assert result["financial_counts"] == {
        "scope": "run_checkpoint",
        "matched": 8,
        "needs_review": 2,
        "not_verified": None,
    }
    assert "private-order" not in str(result) and "do-not-return" not in str(result)


def test_running_expired_lease_and_deadline_are_distinct():
    row = run(status="running", termination_reason=None)
    assert status.run_snapshot(row, NOW)["execution_state"] == "running"
    row.lease_until = NOW - timedelta(seconds=1)
    assert status.run_snapshot(row, NOW)["execution_state"] == "lease_expired"
    row.deadline_at = NOW - timedelta(seconds=1)
    assert status.run_snapshot(row, NOW)["execution_state"] == "deadline_expired"


def test_pending_work_respects_original_queue_age():
    row = run(status="pending", config_snapshot={"deadline_seconds": 900}, deadline_at=NOW - timedelta(days=1))
    assert status.run_snapshot(row, NOW)["execution_state"] == "queue_expired"


def test_read_backoff_and_hard_stop_use_continuation_contract():
    row = run()
    row.progress_json = {
        "processed": 0,
        "read_stop_reason": "retry_limit",
        "read_stop_run_id": str(row.id),
        "last_read_failure": {
            "code": "replica_transport_failed",
            "retryable": True,
            "resolved": False,
            "observed_at": row.finished_at.isoformat(),
        },
    }
    result = status.continuation_status(row, NOW)
    assert result["state"] == "waiting_for_retry"
    assert result["eligible_at"] == (row.finished_at + timedelta(minutes=5)).isoformat()
    assert status.continuation_status(row, NOW + timedelta(minutes=4))["state"] == "eligible"
    row.progress_json["continuation_read_retry_count"] = 3
    assert status.continuation_status(row, NOW)["reason"] == "read_retry_limit"
    row.progress_json["continuation_part"] = 96
    assert status.continuation_status(row, NOW)["reason"] == "part_limit"


def test_inherited_or_resolved_failure_is_not_a_new_global_stop():
    row = run(
        progress_json={
            "processed": 10,
            "last_read_failure": {
                "code": "replica_transport_failed",
                "resolved": False,
                "retryable": True,
                "observed_at": (NOW - timedelta(days=2)).isoformat(),
            },
        }
    )
    assert status.run_snapshot(row, NOW)["last_read_failure"]["blocking"] is False
    assert status.continuation_status(row, NOW)["state"] == "eligible"
    row.progress_json["last_read_failure"]["resolved"] = True
    assert status.run_snapshot(row, NOW)["last_read_failure"]["resolved"] is True


def test_hard_audit_block_is_not_overridden_by_productivity():
    assert status.continuation_status(run(), NOW, blocked={"reason": "actor_unavailable"}) == {
        "state": "blocked",
        "reason": "actor_unavailable",
        "eligible_at": None,
    }
    assert status.continuation_status(run(), NOW, blocked={"reason": "no_progress"})["state"] == "blocked"


def test_backoff_does_not_claim_eligibility_after_cycle_expiry():
    r = run()
    r.progress_json = {
        "processed": 0,
        "continuation_started_at": (NOW - timedelta(hours=23, minutes=59)).isoformat(),
        "read_stop_reason": "retry_limit",
        "read_stop_run_id": str(r.id),
        "last_read_failure": {"code": "replica_transport_failed", "retryable": True, "resolved": False},
    }
    assert status.continuation_status(r, NOW)["reason"] == "cycle_expired"
    r.progress_json.pop("continuation_started_at")
    assert status.continuation_status(r, NOW, blocked={"reason": "no_progress"})["state"] == "waiting_for_retry"


def test_auth_reconnect_is_not_claimed_to_bypass_finite_caps():
    r = run(termination_reason="error")
    r.progress_json = {
        "continuation_part": 96,
        "last_read_failure": {
            "code": "netsuite_upstream_http_401",
            "resolved": False,
            "observed_at": (NOW - timedelta(minutes=3)).isoformat(),
        },
    }
    assert status.continuation_status(r, NOW)["reason"] == "part_limit"


@pytest.mark.parametrize("reason", ["error", "stall", "done"])
def test_interval_schedule_does_not_promise_a_second_dispatch_in_same_bucket(reason):
    c = config(mapping_json={})
    r = run(termination_reason=reason, created_at=NOW)
    result = status._next_action(c, r, [], {"status": "not_applicable"}, status.schedule(c, NOW), None, NOW)
    assert result["kind"] == "scheduled_check"


def test_live_failure_is_distinguished_from_historical_failure():
    r = run(
        termination_reason="error",
        progress_json={
            "last_read_failure": {
                "code": "replica_query_failed",
                "resolved": False,
                "observed_at": (NOW - timedelta(minutes=3)).isoformat(),
            }
        },
    )
    assert status.run_snapshot(r, NOW)["last_read_failure"]["blocking"] is True


def test_operational_table_survives_streamed_and_nonstreamed_condensation():
    import json

    from app.services.chat.orchestrator import _intercept_tool_result
    from app.services.transaction_ops.chat_evidence import condense_status

    result = {
        "success": True,
        "source": "stored_reconciliation_state",
        "observed_at": NOW.isoformat(),
        "entities": [],
        "truncated": False,
        "next_offset": None,
    }
    result.update(status.chat_table(result))
    event, rendered, condensed = _intercept_tool_result("transaction_ops_status", json.dumps(result))
    assert event == "data_table" and rendered["columns"] == result["columns"]
    assert json.loads(condensed)["observed_at"] == NOW.isoformat()
    assert json.loads(condense_status(result))["source"] == "stored_reconciliation_state"
    assert "not financial certification" in json.loads(condensed)["note"]


async def seed(db, actor):
    c = await seed_config(
        db,
        actor.tenant_id,
        actor,
        mapping_json={
            "reference_field": "tranid",
            "currency_minor_units": {"USD": 2},
            "action_mode": "detect_only",
            "reconciliation_policy": {"timezone_name": "America/Los_Angeles"},
        },
    )
    c.enabled = c.schedule_enabled = True
    r = TransactionRun(
        tenant_id=actor.tenant_id,
        config_id=c.id,
        origin="schedule",
        work_key=uuid4().hex,
        params_json={
            "window_start": "2026-09-26T07:00:00+00:00",
            "window_end": "2026-09-27T07:00:00+00:00",
            "evaluation_key": "daily:2026-09-27T07:00:00+00:00",
        },
        config_snapshot=state_service._config_snapshot(c),
        status="finished",
        termination_reason="done",
        max_api_calls=100,
        max_orders=100,
        created_at=NOW - timedelta(hours=3),
        finished_at=NOW - timedelta(hours=2),
        deadline_at=NOW,
        progress_json={
            "scan_complete": True,
            "refund_scan_complete": True,
            "destination_scan_complete": True,
            "processed": 10,
            "matched": 8,
            "needs_review": 2,
            "not_verified": 0,
        },
    )
    db.add(r)
    await db.flush()
    return c, r


async def test_coverage_boundary_tenant_isolation_and_no_writes(db, admin_user, admin_user_b):
    actor = admin_user[0]
    c, r = await seed(db, actor)
    statements = []
    conn = await db.connection()

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(conn.sync_connection, "before_cursor_execute", capture)
    try:
        before = await status.operational_status(db, actor.tenant_id, now=NOW)
        after = await status.operational_status(db, actor.tenant_id, now=NOW + timedelta(hours=1))
        other = await status.operational_status(db, admin_user_b[0].tenant_id, now=NOW)
    finally:
        event.remove(conn.sync_connection, "before_cursor_execute", capture)
    entity = before["entities"][0]
    assert entity["coverage"]["status"] == "up_to_date"
    assert entity["coverage"]["checked_through"] == "2026-09-26"
    assert entity["coverage"]["expected_checked_through"] == "2026-09-26"
    assert entity["next_action"]["kind"] == "scheduled_check"
    assert after["entities"][0]["coverage"]["status"] == "behind"
    assert after["entities"][0]["next_action"]["kind"] == "catch_up"
    assert other["entities"] == []
    assert not set(statements) & {"INSERT", "UPDATE", "DELETE"}
    assert len(statements) < 25  # includes tenant SET LOCAL and both snapshots
    assert entity["latest_schedule"]["financial_counts"]["needs_review"] == 2


async def test_stale_collection_wait_is_labelled_and_does_not_leak_another_tenant(db, admin_user, admin_user_b):
    c, completed = await seed(db, admin_user[0])
    _, foreign = await seed(db, admin_user_b[0])
    waiter = TransactionRun(
        tenant_id=c.tenant_id,
        config_id=c.id,
        origin="schedule",
        work_key=uuid4().hex,
        params_json=completed.params_json,
        config_snapshot=completed.config_snapshot,
        status="pending",
        max_api_calls=100,
        max_orders=100,
        created_at=NOW - timedelta(minutes=1),
        deadline_at=NOW,
        progress_json={"collection_wait": {"run_id": str(foreign.id), "reason": "overlapping_collection"}},
    )
    db.add(waiter)
    await db.flush()
    result = await status.operational_status(db, c.tenant_id, now=NOW)
    wait = result["entities"][0]["active_runs"][0]["collection_wait"]
    assert wait["owner"] is None
    assert str(foreign.id) not in str(result)
    waiter.progress_json = {"collection_wait": {"run_id": str(completed.id), "reason": "overlapping_collection"}}
    await db.flush()
    result = await status.operational_status(db, c.tenant_id, now=NOW)
    wait = result["entities"][0]["active_runs"][0]["collection_wait"]
    assert wait["owner"]["status"] == "finished"
    assert wait["basis"] == "recorded_wait_requires_scheduler_recheck"
    assert result["entities"][0]["next_action"]["kind"] == "collection_recheck"


async def test_api_is_gated_and_uses_shared_service(client, db, admin_user, monkeypatch):
    actor, headers = admin_user
    url = "/api/v1/transaction-ops/operational-status"
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers=headers)).status_code == 403
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, actor.tenant_id, flag)
    read = AsyncMock(
        return_value={
            "observed_at": NOW.isoformat(),
            "entities": [],
            "truncated": False,
            "next_offset": None,
            "source": "stored_reconciliation_state",
        }
    )
    monkeypatch.setattr(status, "operational_status", read)
    response = await client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert read.await_args.args[1] == actor.tenant_id
    assert (await client.get(url + "?limit=51", headers=headers)).status_code == 422
    assert (await client.get(url + "?config_id=not-a-uuid", headers=headers)).status_code == 422


async def test_config_pagination_scope_and_active_rows_are_bounded(db, admin_user):
    actor = admin_user[0]
    first, _ = await seed(db, actor)
    second, _ = await seed(db, actor)
    page = await status.operational_status(db, actor.tenant_id, limit=1, now=NOW)
    assert len(page["entities"]) == 1 and page["next_offset"] == 1 and page["truncated"]
    next_page = await status.operational_status(db, actor.tenant_id, limit=1, offset=1, now=NOW)
    assert not next_page["truncated"]
    assert {page["entities"][0]["config_id"], next_page["entities"][0]["config_id"]} == {str(first.id), str(second.id)}
    assert (await status.operational_status(db, actor.tenant_id, config_id=uuid4(), now=NOW))["entities"] == []
    for i in range(8):
        db.add(
            TransactionRun(
                tenant_id=actor.tenant_id,
                config_id=first.id,
                origin="manual",
                work_key=uuid4().hex,
                status="pending",
                params_json={},
                config_snapshot={},
                max_api_calls=10,
                max_orders=10,
                deadline_at=NOW,
                progress_json={},
            )
        )
    await db.flush()
    scoped = await status.operational_status(db, actor.tenant_id, config_id=first.id, now=NOW)
    assert len(scoped["entities"]) == 1
    assert len(scoped["entities"][0]["active_runs"]) == 5
    assert scoped["entities"][0]["active_runs_truncated"]
