"""A saved coverage receipt can advance a review without claiming a fresh scan."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.transaction_ops import daily_evidence, period_review, runner, scheduler
from tests.test_transaction_daily_evidence import daily
from tests.test_transaction_period_review_api import ready


@pytest.mark.parametrize("mode", ["current", "saved"])
async def test_v2_partial_coverage_receipts_continue_to_the_uncovered_day(db, admin_user, monkeypatch, mode):
    actor = admin_user[0]
    config = await ready(db, actor, monkeypatch)
    root = await period_review.create_review(
        db,
        actor.tenant_id,
        config.id,
        period_review.PeriodReview(evaluation_key=uuid4(), period="last_month", evidence_mode=mode),
        actor=actor,
    )
    assert root.config_snapshot["destination_discovery_version"] == 2
    source = await daily(db, root)
    original_finished_at = source.finished_at
    provider = AsyncMock(side_effect=AssertionError("Covered slices must not read providers"))
    part = root
    for _ in range(2):
        result = await runner.run_investigation(
            db,
            actor.tenant_id,
            part.id,
            _page_reader=provider,
            _source_reader=provider,
            _target_reader=provider,
            _enabled=AsyncMock(return_value=True),
        )
        assert result["termination_reason"] == "done"
        assert part.progress_json["review_coverage_complete"] is False
        assert not daily_evidence.scan_complete(part)  # No invented dependency scan.
        assert part.api_calls_used == part.orders_used == 0
        assert part.id in await scheduler._recovery_ids(db, actor.tenant_id, datetime.now(timezone.utc))
        child = await period_review.continue_review(db, actor.tenant_id, part.id)
        assert child is not None
        assert child.params_json["window_start"] == part.params_json["window_end"]
        assert child.params_json.get("evidence_mode", "current") == mode
        assert (await period_review.continue_review(db, actor.tenant_id, part.id)).id == child.id
        part = child
    assert part.status == "pending" and part.progress_json == {}
    assert part.params_json["window_start"] == source.params_json["window_end"]
    summary = await period_review.review_status(db, actor.tenant_id, root.id)
    assert not summary["complete"] and summary["status"] == "running"
    assert summary["completed_slices"] == 2
    assert source.finished_at == original_finished_at
    provider.assert_not_awaited()


@pytest.mark.parametrize("source_ids", [[], [str(uuid4())], ["not-a-uuid"]])
async def test_unverifiable_receipt_cannot_advance(db, admin_user, monkeypatch, source_ids):
    actor = admin_user[0]
    config = await ready(db, actor, monkeypatch)
    root = await period_review.create_review(
        db,
        actor.tenant_id,
        config.id,
        period_review.PeriodReview(evaluation_key=uuid4(), period="last_month", evidence_mode="saved"),
        actor=actor,
    )
    # Valid unrelated evidence must not rescue an empty or invalid receipt.
    await daily(db, root)
    root.status, root.termination_reason = "finished", "done"
    root.finished_at = datetime.now(timezone.utc)
    root.progress_json = {
        "scan_complete": True,
        "refund_scan_complete": True,
        "destination_scan_complete": True,
        "reused_observation_run_ids": source_ids,
        "review_coverage_complete": False,
    }
    await db.flush()
    assert await period_review.continue_review(db, actor.tenant_id, root.id) is None
