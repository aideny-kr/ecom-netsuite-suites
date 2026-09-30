"""Provider-free cached members with real DB leases, atomic cursors and isolation."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.models.transaction_ops import TransactionCase, TransactionFinding
from app.schemas.transaction_runs import ProgressUpdate, RunCreate
from app.services.transaction_ops import cached_review, daily_evidence, period_review, runner
from app.services.transaction_ops import state_service as state
from tests.test_policy_equivalence import NOW, STAMP, saved_report, snapshots
from tests.test_policy_replay import seed


def request(mode="saved"):
    return period_review.PeriodReview(
        evaluation_key=uuid4(),
        period="custom",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 1),
        evidence_mode=mode,
    )


async def setup(db, actor):
    source, target, old = await seed(db, actor)
    run = await period_review.create_review(db, actor.tenant_id, target.id, request(), actor=actor)
    run.progress_json = {
        "phase": "orders",
        "pending_refs": ["R123456780"],
        "processed": 0,
        "scan_complete": True,
        "refund_scan_complete": True,
        "destination_scan_complete": True,
        "dependency_scan_complete": True,
        "dependency_index_seed": {"complete": True},
    }
    await db.flush()
    return source, target, old, run


@pytest.mark.parametrize("mutation", ["affected", "incomplete", "scope", "policy", "newer", "future"])
def test_unproven_or_changed_inputs_require_fresh_reads(mutation):
    before, after = snapshots()
    report = saved_report()
    minimum = None
    if mutation == "affected":
        from tests.test_policy_equivalence import link

        report["refund_evidence"]["target"]["request_links"] = [link("4")]
    elif mutation == "incomplete":
        report["refund_evidence"]["source"]["complete"] = False
    elif mutation == "scope":
        after["subsidiary_id"] = "99"
    elif mutation == "policy":
        after["mapping_json"]["reference_field"] = "different"
    elif mutation == "newer":
        minimum = NOW
    elif mutation == "future":
        report["_observation"]["observed_at"] = (NOW + timedelta(days=1)).isoformat()
    assert cached_review.equivalent(report, before, after, now=NOW, minimum_observed_at=minimum) is None


def test_same_policy_and_supported_revision_keep_original_numeric_results():
    before, after = snapshots()
    report = saved_report()
    for target in (before, after):
        result = cached_review.equivalent(report, before, target, now=NOW)
        assert result["balance"] == report["balance"]
        assert result["original_observed_at"] == STAMP


async def test_runner_reuses_discovered_member_without_provider_jev_or_case_changes(db, admin_user, monkeypatch):
    actor = admin_user[0]
    _, _, _, run = await setup(db, actor)
    before = await db.scalar(
        select(func.count()).select_from(TransactionCase).where(TransactionCase.tenant_id == actor.tenant_id)
    )
    provider = AsyncMock(side_effect=AssertionError("Cached member must not read providers"))
    from app.services.transaction_ops import hybrid_classification

    monkeypatch.setattr(hybrid_classification, "classify_report", provider)
    result = await runner.run_investigation(
        db,
        actor.tenant_id,
        run.id,
        _source_reader=provider,
        _target_reader=provider,
        _page_reader=provider,
        _source_refunds_reader=provider,
        _target_refunds_reader=provider,
        _refund_page_reader=provider,
        _dependency_page_reader=provider,
        _dependency_owner_reader=provider,
    )
    assert result["termination_reason"] == "done"
    assert result["processed"] == 1
    assert run.api_calls_used == run.orders_used == 0
    assert run.progress_json["cached_results_reused"] == 1
    provider.assert_not_awaited()
    finding = await db.scalar(select(TransactionFinding).where(TransactionFinding.run_id == run.id))
    assert finding.report_json["_observation"]["observed_at"] == STAMP
    assert finding.report_json["cached_evidence"]["basis"] == "historical_as_observed"
    assert "case_id" not in finding.report_json and "hybrid_classification" not in finding.report_json
    assert before == await db.scalar(
        select(func.count()).select_from(TransactionCase).where(TransactionCase.tenant_id == actor.tenant_id)
    )
    summary = await period_review.review_status(db, actor.tenant_id, run.id)
    assert summary["complete"] and summary["cached_results_reused"] == 1
    assert summary["comparison_basis"] == "saved_evidence_with_targeted_refresh"
    # A fresh/daily review must never consume cached review coverage.
    fresh = await period_review.create_review(db, actor.tenant_id, run.config_id, request("current"), actor=actor)
    assert fresh.status == "pending"
    assert not await daily_evidence.completed_observation_windows(db, fresh, request_span(fresh))
    # The same saved review can reuse its complete discovery with no queue work.
    again = await period_review.create_review(db, actor.tenant_id, run.config_id, request(), actor=actor)
    assert again.status == "finished" and again.api_calls_used == 0


def request_span(run):
    from app.schemas.transaction_runs import ReviewSpan

    return ReviewSpan.model_validate(run.params_json["review"])


async def test_candidates_are_scoped_and_affected_unknown_members_fall_back(db, admin_user, tenant_b):
    actor = admin_user[0]
    _, _, _, run = await setup(db, actor)
    refs = [f"R12345678{i}" for i in range(3)]
    cache = cached_review.CachedReview(db, actor.tenant_id, run)
    await cache.prepare(refs, run.progress_json, now=NOW)
    assert len(cache.prefix(refs)) == 1
    assert cache.prepared[refs[1]] is cache.prepared[refs[2]] is None
    assert cache.fresh_prefix_size(refs[1:]) == 2
    with pytest.raises(state.StateError, match="not_found"):
        await cached_review.CachedReview(db, tenant_b.id, run).prepare(refs, run.progress_json, now=NOW)
    await cache.prepare(
        refs, {**run.progress_json, "phase": "destination", "pending_evidence_since": NOW.isoformat()}, now=NOW
    )
    assert not cache.prefix(refs)  # deletion/change after the old observation


async def test_changed_original_and_lost_lease_cannot_publish_cached_results(db, admin_user):
    actor = admin_user[0]
    _, _, _, run = await setup(db, actor)
    now = datetime.now(timezone.utc)
    token = await state.claim_run(db, actor.tenant_id, run.id, now=now)
    cache = cached_review.CachedReview(db, actor.tenant_id, run)
    await cache.prepare(run.progress_json["pending_refs"], run.progress_json, now=now)
    reports = cache.prefix(run.progress_json["pending_refs"])
    checkpoint = ProgressUpdate(progress_json={**run.progress_json, "processed": 1, "pending_refs": []})
    original = await db.get(TransactionFinding, reports[0]["cached_evidence"]["finding_id"])
    original.report_json = {**original.report_json, "changed": True}
    await db.flush()
    assert not await cached_review.persist(
        db, actor.tenant_id, run.id, reports, lease_token=token, checkpoint=checkpoint, now=now
    )
    assert run.progress_json["processed"] == 0
    with pytest.raises(state.StateError, match="run_lease_lost"):
        await cached_review.persist(
            db, actor.tenant_id, run.id, reports, lease_token=uuid4(), checkpoint=checkpoint, now=now
        )
    assert not await db.scalar(select(TransactionFinding.id).where(TransactionFinding.run_id == run.id))


async def test_atomic_replay_rolls_back_copy_and_checkpoint(db, admin_user, monkeypatch):
    actor = admin_user[0]
    _, _, _, run = await setup(db, actor)
    now = datetime.now(timezone.utc)
    token = await state.claim_run(db, actor.tenant_id, run.id, now=now)
    run_id, tenant_id = run.id, actor.tenant_id
    cache = cached_review.CachedReview(db, actor.tenant_id, run)
    await cache.prepare(run.progress_json["pending_refs"], run.progress_json, now=now)
    reports = cache.prefix(run.progress_json["pending_refs"])
    checkpoint = ProgressUpdate(progress_json={**run.progress_json, "processed": 1, "pending_refs": []})
    commit = state._commit
    monkeypatch.setattr(state, "_commit", AsyncMock(side_effect=RuntimeError("interrupted")))
    with pytest.raises(RuntimeError, match="interrupted"):
        await cached_review.persist(db, tenant_id, run_id, reports, lease_token=token, checkpoint=checkpoint, now=now)
    await db.rollback()
    monkeypatch.setattr(state, "_commit", commit)
    run = await state.get_run(db, tenant_id, run_id)
    assert run.progress_json["processed"] == 0
    assert not await db.scalar(select(TransactionFinding.id).where(TransactionFinding.run_id == run.id))
    assert await cached_review.persist(
        db, tenant_id, run_id, reports, lease_token=token, checkpoint=checkpoint, now=now
    )
    assert run.progress_json["processed"] == 1


def test_saved_mode_cannot_be_scheduled_or_used_for_exact_order_writes():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RunCreate(evaluation_key="test", origin="schedule", order_references=["R123456789"], evidence_mode="saved")


async def test_reused_rows_and_export_selection_keep_original_dates(db, admin_user):
    from app.services.transaction_ops.workspace_results import review_page, selected_evidence

    actor = admin_user[0]
    _, _, _, run = await setup(db, actor)
    provider = AsyncMock(side_effect=AssertionError("unexpected provider read"))
    await runner.run_investigation(db, actor.tenant_id, run.id, _source_reader=provider, _target_reader=provider)
    page = await review_page(db, actor.tenant_id, [str(run.id)])
    assert page["summary"]["checked"] == 1
    assert page["items"][0]["observed_at"] == STAMP
    assert page["items"][0]["cached_evidence"]["basis"] == "historical_as_observed"
    query, scopes = await selected_evidence(db, actor.tenant_id, [str(run.id)])
    row = (await db.execute(select(query))).mappings().one()
    assert row["updated_at"].isoformat() == STAMP
    assert scopes[0]["evidence_mode"] == "saved"


async def test_mode_is_bound_to_idempotency_key(db, admin_user):
    actor = admin_user[0]
    _, target, _, pending = await setup(db, actor)
    body = request().model_copy(update={"evaluation_key": UUID(pending.params_json["evaluation_key"])})
    first = await period_review.create_review(db, actor.tenant_id, target.id, body, actor=actor)
    assert (await period_review.create_review(db, actor.tenant_id, target.id, body, actor=actor)).id == first.id
    with pytest.raises(state.StateError, match="evaluation_key_conflict"):
        await period_review.create_review(
            db, actor.tenant_id, target.id, body.model_copy(update={"evidence_mode": "current"}), actor=actor
        )
