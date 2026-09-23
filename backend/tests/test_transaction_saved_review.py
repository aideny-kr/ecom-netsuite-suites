"""Historical reports reuse scoped collected evidence without a provider rescan."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from app.services.transaction_ops import period_review, runner, scheduler, state_service
from tests.test_transaction_period_review_api import ready
from tests.test_transaction_review_results import add


@pytest.fixture
def publisher(monkeypatch):
    publish = Mock(return_value=True)
    monkeypatch.setattr(scheduler, "publish_investigation", publish)
    return publish


async def saved_day(db, actor, monkeypatch, *, destination=True):
    config = await ready(db, actor, monkeypatch)
    request = period_review.PeriodReview(evaluation_key=uuid4(), period="yesterday")
    run = await period_review.create_review(db, actor.tenant_id, config.id, request, actor=actor)
    run.status, run.termination_reason = "finished", "done"
    run.finished_at = datetime.now(timezone.utc)
    run.progress_json = {
        "scan_complete": True,
        "refund_scan_complete": True,
        "destination_scan_complete": destination,
    }
    await db.flush()
    await add(db, actor, run, "R123456789", "matched", run.created_at)
    return config, run


async def test_new_report_request_materializes_saved_results_without_dispatch(
    client, db, admin_user, monkeypatch, publisher
):
    actor, headers = admin_user
    config, original = await saved_day(db, actor, monkeypatch)
    publisher.reset_mock()
    body = {"period": "yesterday", "evaluation_key": str(uuid4())}
    response = await client.post(f"/api/v1/transaction-ops/configs/{config.id}/review", json=body, headers=headers)
    assert response.status_code == 202, response.text
    data = response.json()
    assert data["id"] != str(original.id)
    assert data["status"] == "finished" and data["termination_reason"] == "done"
    assert data["api_calls_used"] == data["orders_used"] == 0
    assert data["progress_json"]["review_coverage_complete"] is True
    assert str(original.id) in data["progress_json"]["reused_observation_run_ids"]
    publisher.assert_not_called()
    run_id = UUID(data["id"])
    results = await period_review.review_results(db, actor.tenant_id, run_id)
    assert results["summary"] == {"checked": 1, "matched": 1, "needs_review": 0, "not_verified": 0}
    assert not await state_service.list_findings(db, actor.tenant_id, run_id)  # No restamping/copying observations.
    status = await period_review.review_status(db, actor.tenant_id, run_id)
    assert status["complete"] and status["source_freshness"] == "unverified"
    assert await period_review.continue_review(db, actor.tenant_id, run_id) is None
    assert run_id not in await scheduler._recovery_ids(db, actor.tenant_id, datetime.now(timezone.utc))
    again = await client.post(f"/api/v1/transaction-ops/configs/{config.id}/review", json=body, headers=headers)
    assert again.json()["id"] == data["id"]
    publisher.assert_not_called()


async def test_missing_destination_coverage_cannot_finish_or_advance(db, admin_user, monkeypatch):
    actor = admin_user[0]
    config, original = await saved_day(db, actor, monkeypatch, destination=False)
    status = await period_review.review_status(db, actor.tenant_id, original.id)
    assert not status["complete"] and status["completed_slices"] == 0
    assert await period_review.continue_review(db, actor.tenant_id, original.id) is None
    new = await period_review.create_review(
        db,
        actor.tenant_id,
        config.id,
        period_review.PeriodReview(evaluation_key=uuid4(), period="yesterday"),
        actor=actor,
    )
    assert new.status == "pending"
    # Useful partial observations remain available, without a false coverage claim.
    assert (await period_review.review_results(db, actor.tenant_id, new.id))["summary"]["checked"] == 1
    assert not (await period_review.review_status(db, actor.tenant_id, new.id))["complete"]


async def test_repeat_receipts_keep_provider_anchors_and_cannot_self_certify(db, admin_user, monkeypatch):
    from sqlalchemy import delete

    from app.models.transaction_ops import TransactionFinding, TransactionRun
    from app.schemas.transaction_runs import ReviewSpan
    from app.services.transaction_ops.daily_evidence import completed_observation_windows

    actor = admin_user[0]
    config, original = await saved_day(db, actor, monkeypatch)
    for _ in range(3):
        receipt = await period_review.create_review(
            db,
            actor.tenant_id,
            config.id,
            period_review.PeriodReview(evaluation_key=uuid4(), period="yesterday"),
            actor=actor,
        )
        assert receipt.status == "finished"
        assert receipt.progress_json["reused_observation_run_ids"] == [str(original.id)]
    span = ReviewSpan.model_validate(receipt.params_json["review"])
    windows = await completed_observation_windows(db, receipt, span)
    assert [row[2] for row in windows] == [str(original.id)]
    # If the collected anchor is removed, receipts cannot uphold one another.
    await db.execute(delete(TransactionFinding).where(TransactionFinding.run_id == original.id))
    await db.execute(delete(TransactionRun).where(TransactionRun.id == original.id))
    assert not await completed_observation_windows(db, receipt, span)
    assert not (await period_review.review_status(db, actor.tenant_id, receipt.id))["complete"]


async def test_failed_period_retry_uses_newly_available_coverage_without_dispatch(
    client, db, admin_user, monkeypatch, publisher
):
    from app.models.transaction_ops import TransactionRun

    actor, headers = admin_user
    config, original = await saved_day(db, actor, monkeypatch)
    failed = TransactionRun(
        tenant_id=actor.tenant_id,
        config_id=config.id,
        config_snapshot=original.config_snapshot,
        params_json={
            **original.params_json,
            "evaluation_key": str(uuid4()),
            "review": {**original.params_json["review"], "id": str(uuid4())},
        },
        initiated_by=actor.id,
        work_key=uuid4().hex,
        origin="manual",
        status="finished",
        termination_reason="error",
        max_api_calls=original.max_api_calls,
        max_orders=original.max_orders,
        deadline_at=original.deadline_at,
        created_at=original.created_at + timedelta(seconds=1),
        finished_at=datetime.now(timezone.utc),
    )
    db.add(failed)
    await db.flush()
    publisher.reset_mock()
    response = await client.post(
        f"/api/v1/transaction-ops/configs/{config.id}/review",
        json={"period": "yesterday", "evaluation_key": str(uuid4())},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    data = response.json()
    assert data["id"] not in (str(original.id), str(failed.id))
    assert data["status"] == "finished" and data["termination_reason"] == "done"
    assert data["progress_json"]["continuation_of"] == str(failed.id)
    assert data["progress_json"]["reused_observation_run_ids"] == [str(original.id)]
    assert data["api_calls_used"] == data["orders_used"] == 0
    publisher.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("subsidiary_id", "999"),
        ("netsuite_account_id", "9999999"),
        ("mapping_json", {"reference_field": "other_field"}),
        ("evidence_contract_version", 2),
    ],
)
async def test_completed_saved_run_cannot_cross_scope_or_policy(db, admin_user, monkeypatch, field, value):
    from app.schemas.transaction_runs import ReviewSpan
    from app.services.transaction_ops.daily_evidence import completed_observation_windows

    actor = admin_user[0]
    _, original = await saved_day(db, actor, monkeypatch)
    # Old run snapshots are immutable. Evaluate a differently scoped view,
    # rather than weakening the DB constraint just to manufacture a fixture.
    different = SimpleNamespace(
        tenant_id=original.tenant_id,
        config_id=original.config_id,
        config_snapshot={**original.config_snapshot, field: value},
        params_json=original.params_json,
    )
    assert not await completed_observation_windows(
        db, different, ReviewSpan.model_validate(original.params_json["review"])
    )


async def test_worker_reuses_completed_manual_slice_with_no_provider_calls(db, admin_user, monkeypatch):
    actor = admin_user[0]
    config, original = await saved_day(db, actor, monkeypatch)
    # A wider requested range still has gaps, but this collected day needs no network work.
    monkeypatch.setattr(period_review, "utc_now", lambda: datetime(2026, 9, 10, 18, tzinfo=timezone.utc))
    old_span = original.params_json["review"]
    start = datetime.fromisoformat(old_span["start"]).date()
    new = await period_review.create_review(
        db,
        actor.tenant_id,
        config.id,
        period_review.PeriodReview(
            evaluation_key=uuid4(), period="custom", start_date=start, end_date=start + timedelta(days=1)
        ),
        actor=actor,
    )
    # The fixture clock must have closed both requested dates.
    assert new.status == "pending"
    providers = AsyncMock(side_effect=AssertionError("Saved coverage must not call upstream"))
    result = await runner.run_investigation(
        db,
        actor.tenant_id,
        new.id,
        _source_reader=providers,
        _target_reader=providers,
        _page_reader=providers,
        _enabled=AsyncMock(return_value=True),
    )
    assert result["termination_reason"] == "done"
    assert new.api_calls_used == new.orders_used == 0
    providers.assert_not_awaited()
    assert new.progress_json["reused_observation_run_ids"] == [str(original.id)]
    assert new.progress_json["reused_daily_run_ids"] == []
    assert not new.progress_json["review_coverage_complete"]
