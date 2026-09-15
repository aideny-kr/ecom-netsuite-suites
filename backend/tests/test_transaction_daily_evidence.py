from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models.transaction_ops import TransactionRun
from app.schemas.transaction_runs import ReviewSpan, RunCreate
from app.services.transaction_ops import daily_evidence, daily_status, period_review, runner
from tests.test_transaction_review_results import add
from tests.test_transaction_review_slices import review


async def daily(db, root, *, params=None, snapshot=None, progress=None, reason="done"):
    span = ReviewSpan.model_validate(root.params_json["review"])
    row = TransactionRun(
        tenant_id=root.tenant_id,
        config_id=root.config_id,
        origin="schedule",
        work_key=uuid4().hex,
        params_json=RunCreate(
            origin="schedule",
            evaluation_key=uuid4().hex,
            window_start=span.start,
            window_end=span.start + timedelta(days=2),
        ).model_dump(mode="json", exclude={"window_basis", "review"})
        | (params or {}),
        config_snapshot=snapshot or root.config_snapshot,
        progress_json={
            "scan_complete": True,
            "refund_scan_complete": True,
            "destination_scan_complete": True,
            **(progress or {}),
        },
        status="finished",
        termination_reason=reason,
        max_api_calls=100,
        max_orders=100,
        deadline_at=root.deadline_at,
        finished_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.flush()
    return row


async def test_reuse_daily_evidence_deduplicates_and_covers_without_new_provider_calls(db, admin_user, monkeypatch):
    actor = admin_user[0]
    _, root = await review(db, actor, monkeypatch)
    row = await daily(db, root)
    await add(db, actor, row, "R123456789", "difference", row.created_at)
    await add(db, actor, root, "R123456789", "matched", row.created_at + timedelta(seconds=1))
    await add(db, actor, row, "R123456788", "difference", row.created_at)
    results = await period_review.review_results(db, actor.tenant_id, root.id)
    assert results["summary"] == {"checked": 2, "matched": 1, "needs_review": 1, "not_verified": 0}
    status = await period_review.review_status(db, actor.tenant_id, root.id)
    assert status["completed_slices"] == 2 and not status["complete"]
    providers = AsyncMock(side_effect=AssertionError("Covered slices must not call a provider"))
    result = await runner.run_investigation(
        db,
        actor.tenant_id,
        root.id,
        _source_reader=providers,
        _target_reader=providers,
        _page_reader=providers,
        _enabled=AsyncMock(return_value=True),
    )
    assert result["termination_reason"] == "done"
    assert root.api_calls_used == root.orders_used == 0
    assert root.progress_json["reused_daily_run_ids"] == [str(row.id)]
    providers.assert_not_awaited()


@pytest.mark.parametrize("change", ["basis", "mapping", "entity", "source", "account", "outside_period"])
async def test_daily_evidence_never_crosses_scope_or_date_basis(db, admin_user, monkeypatch, change):
    actor = admin_user[0]
    _, root = await review(db, actor, monkeypatch)
    snapshot = dict(root.config_snapshot)
    params = {}
    if change == "basis":
        params["window_basis"] = "completed_at"
    elif change == "mapping":
        snapshot["mapping_json"] = {**snapshot["mapping_json"], "reference_field": "other"}
    elif change == "entity":
        snapshot["subsidiary_id"] = "999"
    elif change == "account":
        snapshot["netsuite_account_id"] = "other"
    elif change == "source":
        snapshot["source_connection_id"] = str(uuid4())
    else:
        params["window_start"] = (
            ReviewSpan.model_validate(root.params_json["review"]).start - timedelta(days=1)
        ).isoformat()
    row = await daily(db, root, params=params, snapshot=snapshot)
    await add(db, actor, row, "R123456789", "matched", row.created_at)
    assert (await period_review.review_results(db, actor.tenant_id, root.id))["summary"]["checked"] == 0
    assert (await period_review.review_status(db, actor.tenant_id, root.id))["completed_slices"] == 0


@pytest.mark.parametrize(
    "progress,reason",
    [({"destination_scan_complete": False}, "done"), ({"refund_scan_complete": False}, "done"), ({}, "budget")],
)
async def test_partial_daily_findings_are_visible_but_do_not_certify_coverage(
    db, admin_user, monkeypatch, progress, reason
):
    actor = admin_user[0]
    _, root = await review(db, actor, monkeypatch)
    row = await daily(db, root, progress=progress, reason=reason)
    await add(db, actor, row, "R123456789", "difference", row.created_at)
    assert (await period_review.review_results(db, actor.tenant_id, root.id))["summary"]["checked"] == 1
    assert (await period_review.review_status(db, actor.tenant_id, root.id))["completed_slices"] == 0


async def test_daily_health_reports_real_completed_window_and_tenant_isolation(
    db, admin_user, admin_user_b, monkeypatch
):
    actor = admin_user[0]
    config, root = await review(db, actor, monkeypatch)
    row = await daily(db, root)
    now = datetime(2026, 9, 8, 18, tzinfo=timezone.utc)
    config.enabled = config.schedule_enabled = True
    await db.flush()
    status = next(
        r for r in await daily_status.daily_status(db, actor.tenant_id, now=now) if r["config_id"] == str(config.id)
    )
    assert status["status"] == "behind" and status["run_id"] == str(row.id)
    assert status["checked_through"] == "2026-08-02"
    assert await daily_status.daily_status(db, admin_user_b[0].tenant_id, now=now) == []


def test_coverage_cannot_bridge_a_gap_or_double_count_overlaps():
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=5)
    windows = [
        (start, start + timedelta(days=2), "a"),
        (start + timedelta(days=1), start + timedelta(days=3), "b"),
        (start + timedelta(days=4), end, "c"),
    ]
    assert daily_evidence.covered_until(start, end, windows) == start + timedelta(days=3)
    assert daily_evidence.covered_days(start, end, windows) == 4


async def test_daily_resume_retains_evidence_and_cycle_budgets(db, admin_user, monkeypatch):
    from app.schemas.transaction_runs import RunCreate
    from app.services.transaction_ops import continuation, scheduler, state_service

    actor = admin_user[0]
    config, root = await review(db, actor, monkeypatch)
    config.schedule_enabled = True
    prior = await daily(db, root, reason="budget", progress={"processed": 20, "continuation_part": 16})
    await add(db, actor, prior, "R123456789", "matched", prior.created_at)
    now = datetime.now(timezone.utc)
    key = scheduler._schedule_key(config, now)
    child = await state_service.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(**(prior.params_json | {"origin": "schedule", "evaluation_key": key})),
        now=now,
        resume_from_run_id=prior.id,
    )
    assert await state_service.unseen_references(db, actor.tenant_id, child.id, ["R123456789", "R123456788"]) == [
        "R123456788"
    ]
    assert child.progress_json["continuation_baseline"]["processed"] == 20
    assert "continuation_part" not in child.progress_json
    assert child.max_api_calls == config.max_api_calls
    with pytest.raises(ValueError, match="no_progress"):
        continuation.next_metadata(child, now + timedelta(seconds=1))
    assert scheduler._cycle_key(config, child) == key


async def test_legacy_scheduled_continuation_inherits_reporting_cycle(db, admin_user, monkeypatch):
    from app.services.transaction_ops import continuation, scheduler

    actor = admin_user[0]
    config, root = await review(db, actor, monkeypatch)
    config.schedule_enabled = True
    prior = await daily(db, root, reason="budget", progress={"processed": 20})
    monkeypatch.setattr(continuation, "enabled", AsyncMock(return_value=True))
    # A manual period review is pending on this config; it must not block daily continuation.
    child = await continuation.continue_budget_run(db, actor.tenant_id, prior.id)
    assert child is not None
    assert child.progress_json["schedule_cycle_key"] == scheduler._cycle_key(config, prior)
    assert child.progress_json["continuation_part"] == 2


def test_daily_coverage_counts_calendar_days_across_fall_dst():
    start = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
    end = datetime(2026, 11, 2, 8, tzinfo=timezone.utc)
    assert daily_evidence.covered_days(start, end, [(start, end, "whole-week")]) == 7


async def test_new_scheduler_run_executes_first_page_with_cycle_metadata(db, admin_user, monkeypatch):
    from app.services.transaction_ops import scheduler, state_service
    from tests.test_transaction_ops_state_db import seed_config

    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    config.enabled = config.schedule_enabled = True
    await db.flush()
    monkeypatch.setattr(
        scheduler.feature_flag_service, "list_tenants_with_flags", AsyncMock(return_value=[actor.tenant_id])
    )
    monkeypatch.setattr(scheduler, "_refresh_sources", AsyncMock(return_value=0))
    monkeypatch.setattr(scheduler, "_recovery_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(scheduler, "_dispatch", AsyncMock())
    result = await scheduler.collect_due_runs(db, datetime.now(timezone.utc))
    assert result["created"] == 1
    run = (await state_service.list_runs(db, actor.tenant_id, config_id=config.id))[0]
    key = run.progress_json["schedule_cycle_key"]
    page = AsyncMock(return_value={"page_complete": True, "page": 1, "total_count": 0, "orders": [], "next_page": None})
    outcome = await runner.run_investigation(
        db, actor.tenant_id, run.id, _page_reader=page, _enabled=AsyncMock(return_value=True)
    )
    assert outcome["termination_reason"] == "done" and outcome["processed"] == 0
    page.assert_awaited_once()
    assert run.api_calls_used == 2
    assert run.progress_json["scan_complete"] is True
    assert run.progress_json["schedule_cycle_key"] == key
