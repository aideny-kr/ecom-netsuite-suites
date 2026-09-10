"""Human period retries preserve the cohort, saved cursor and immutable attempts."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import period_review
from app.services.transaction_ops import state_service as state
from tests.test_transaction_period_review_api import ready, routes  # noqa: F401
from tests.test_transaction_review_slices import finish


async def stopped_review(db, actor, monkeypatch, reason="error"):
    config = await ready(db, actor, monkeypatch)
    request = period_review.PeriodReview(evaluation_key=uuid4(), period="last_week")
    root = await period_review.create_review(db, actor.tenant_id, config.id, request, actor=actor)
    await finish(db, root)
    failed = await period_review.continue_review(db, actor.tenant_id, root.id)
    failed.api_calls_used = 45
    failed.orders_used = 4
    failed.progress_json = {
        "last_source_id": 1234,
        "pending_refs": ["R123456789"],
        "phase": "orders",
        "scan_complete": False,
        "refund_scan_complete": False,
        "processed": 14,
        "matched": 12,
        "scan_count": 18,
        "continuation_part": 7,
        "continuation_root_id": str(failed.id),
        "continuation_started_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
    }
    failed.status, failed.termination_reason = "finished", reason
    failed.finished_at = datetime.now(timezone.utc)
    await db.flush()
    return config, root, failed


async def test_existing_review_action_resumes_error_and_retries_idempotently(client, db, admin_user, monkeypatch):
    actor, headers = admin_user
    config, root, failed = await stopped_review(db, actor, monkeypatch)
    before = dict(failed.progress_json)
    body = {"period": "last_week", "evaluation_key": str(uuid4())}
    url = f"/api/v1/transaction-ops/configs/{config.id}/review"
    response = await client.post(url, json=body, headers=headers)
    assert response.status_code == 202, response.text
    data = response.json()
    assert data["params_json"]["review"] == root.params_json["review"]
    assert data["params_json"]["window_start"] == failed.params_json["window_start"]
    assert data["id"] != str(failed.id) and data["status"] == "pending"
    assert data["api_calls_used"] == data["orders_used"] == 0
    assert data["progress_json"]["last_source_id"] == 1234
    assert data["progress_json"]["pending_refs"] == ["R123456789"]
    assert data["progress_json"]["review_attempt"] == 1
    assert data["progress_json"]["continuation_of"] == str(failed.id)
    assert "continuation_started_at" not in data["progress_json"]
    assert data["progress_json"]["continuation_baseline"]["processed"] == 14
    assert (await client.post(url, json=body, headers=headers)).json()["id"] == data["id"]
    await db.refresh(failed)
    assert failed.status == "finished" and failed.termination_reason == "error"
    assert failed.progress_json == before and failed.api_calls_used == 45
    summary = await period_review.review_status(db, actor.tenant_id, root.id)
    assert summary["completed_slices"] == 1 and summary["status"] == "running"
    assert summary["slices"][-1]["run_id"] == data["id"]
    resumed = await state.get_run(db, actor.tenant_id, UUID(data["id"]))
    resumed.progress_json = {**resumed.progress_json, "scan_complete": True, "refund_scan_complete": True}
    resumed.status, resumed.termination_reason = "finished", "done"
    resumed.finished_at = datetime.now(timezone.utc)
    await db.flush()
    summary = await period_review.review_status(db, actor.tenant_id, root.id)
    assert summary["completed_slices"] == 2
    assert (await client.post(url, json=body, headers=headers)).json()["id"] == data["id"]


async def test_retry_does_not_reset_automatic_productivity_budget(db, admin_user, monkeypatch):
    from app.services.transaction_ops.continuation import next_metadata

    actor = admin_user[0]
    config, _, failed = await stopped_review(db, actor, monkeypatch)
    resumed = await period_review.create_review(
        db,
        actor.tenant_id,
        config.id,
        period_review.PeriodReview(evaluation_key=uuid4(), period="last_week"),
        actor=actor,
    )
    assert resumed.progress_json["last_source_id"] == failed.progress_json["last_source_id"]
    with pytest.raises(ValueError, match="no_progress"):
        next_metadata(resumed, datetime.now(timezone.utc))


@pytest.mark.parametrize(
    "origin,automatic,human_retry",
    [
        ("manual", False, False),
        ("schedule", False, True),
        ("chat", False, True),
        ("manual", True, True),
    ],
)
async def test_error_resume_is_exclusive_to_explicit_manual_period_recovery(
    db,
    admin_user,
    monkeypatch,
    origin,
    automatic,
    human_retry,
):
    actor = admin_user[0]
    config, _, failed = await stopped_review(db, actor, monkeypatch)
    config.schedule_enabled = True
    await db.flush()
    with pytest.raises(state.StateError, match="invalid_run_continuation"):
        await state.create_run(
            db,
            actor.tenant_id,
            config.id,
            RunCreate(**{**failed.params_json, "origin": origin, "evaluation_key": str(uuid4())}),
            actor=actor,
            resume_from_run_id=failed.id,
            automatic_continuation=automatic,
            human_retry=human_retry,
        )


async def test_different_period_does_not_reuse_failed_cohort(db, admin_user, monkeypatch):
    actor = admin_user[0]
    config, _, failed = await stopped_review(db, actor, monkeypatch)
    run = await period_review.create_review(
        db,
        actor.tenant_id,
        config.id,
        period_review.PeriodReview(evaluation_key=uuid4(), period="last_month"),
        actor=actor,
    )
    assert run.params_json["review"] != failed.params_json["review"]
    assert run.progress_json == {}


async def test_pending_retry_rejects_another_click_and_old_budget_cannot_fork(client, db, admin_user, monkeypatch):
    from app.services.transaction_ops.continuation import continuation_result, continue_budget_run
    from app.services.transaction_ops.scheduler import _recovery_ids

    actor, headers = admin_user
    config, _, failed = await stopped_review(db, actor, monkeypatch, reason="budget")
    request = period_review.PeriodReview(evaluation_key=uuid4(), period="last_week")
    resumed = await period_review.create_review(db, actor.tenant_id, config.id, request, actor=actor)
    response = await client.post(
        f"/api/v1/transaction-ops/configs/{config.id}/review",
        json={"period": "last_week", "evaluation_key": str(uuid4())},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "review_already_running"
    child, _ = await continuation_result(db, actor.tenant_id, failed.id)
    assert child.id == resumed.id
    assert (await continue_budget_run(db, actor.tenant_id, failed.id)).id == resumed.id
    assert failed.id not in await _recovery_ids(db, actor.tenant_id, datetime.now(timezone.utc))


async def test_retry_after_automatic_child_error_keeps_latest_cursor(db, admin_user, monkeypatch):
    from app.services.transaction_ops.continuation import continue_budget_run

    actor = admin_user[0]
    config, root, _ = await stopped_review(db, actor, monkeypatch)
    first = await period_review.create_review(
        db,
        actor.tenant_id,
        config.id,
        period_review.PeriodReview(evaluation_key=uuid4(), period="last_week"),
        actor=actor,
    )
    first.progress_json = {**first.progress_json, "last_source_id": 1250, "scan_count": 19}
    first.status, first.termination_reason = "finished", "budget"
    first.finished_at = datetime.now(timezone.utc)
    await db.flush()
    child = await continue_budget_run(db, actor.tenant_id, first.id)
    assert child.progress_json["review_attempt"] == 1
    assert child.progress_json["continuation_part"] == 2
    child.progress_json = {**child.progress_json, "last_source_id": 1270}
    child.status, child.termination_reason = "finished", "error"
    child.finished_at = datetime.now(timezone.utc)
    await db.flush()
    second = await period_review.create_review(
        db,
        actor.tenant_id,
        config.id,
        period_review.PeriodReview(evaluation_key=uuid4(), period="last_week"),
        actor=actor,
    )
    assert second.progress_json["last_source_id"] == 1270
    assert second.progress_json["review_attempt"] == 2
    assert second.progress_json["continuation_of"] == str(child.id)
    summary = await period_review.review_status(db, actor.tenant_id, root.id)
    assert summary["current_run_id"] == str(second.id)


async def test_foreign_tenant_and_disabled_configuration_cannot_retry(
    client,
    db,
    admin_user,
    admin_user_b,
    monkeypatch,
):
    from app.schemas.transaction_runs import ConfigControl
    from tests.conftest import enable_feature_flag

    actor, headers = admin_user
    other, other_headers = admin_user_b
    config, _, _ = await stopped_review(db, actor, monkeypatch)
    for flag in ("celigo", "reconciliation"):
        await enable_feature_flag(db, other.tenant_id, flag)
    url = f"/api/v1/transaction-ops/configs/{config.id}/review"
    body = {"period": "last_week", "evaluation_key": str(uuid4())}
    assert (await client.post(url, json=body, headers=other_headers)).status_code == 404
    await state.control_config(
        db,
        actor.tenant_id,
        config.id,
        ConfigControl(enabled=False, schedule_enabled=False),
        actor=actor,
    )
    response = await client.post(url, json=body, headers=headers)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "config_disabled"


async def test_unrelated_daily_check_does_not_block_on_demand_period_recovery(db, admin_user, monkeypatch):
    actor = admin_user[0]
    config, _, failed = await stopped_review(db, actor, monkeypatch)
    config.schedule_enabled = True
    await db.flush()
    daily = await state.create_run(
        db,
        actor.tenant_id,
        config.id,
        RunCreate(
            origin="schedule",
            evaluation_key="daily-check",
            window_start=datetime(2026, 9, 6, 7, tzinfo=timezone.utc),
            window_end=datetime(2026, 9, 8, 7, tzinfo=timezone.utc),
        ),
    )
    resumed = await period_review.create_review(
        db,
        actor.tenant_id,
        config.id,
        period_review.PeriodReview(evaluation_key=uuid4(), period="last_week"),
        actor=actor,
    )
    assert resumed.progress_json["continuation_of"] == str(failed.id)
    assert resumed.progress_json["last_source_id"] == 1234
    assert daily.status == resumed.status == "pending" and daily.id != resumed.id
