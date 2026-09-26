"""Daily/manual coverage sharing must avoid I/O without inventing fresh evidence."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.transaction_ops import TransactionRun
from app.schemas.transaction_runs import ReviewSpan
from app.services.transaction_ops import daily_evidence, daily_status, runner, scheduler
from tests.test_transaction_daily_evidence import daily
from tests.test_transaction_review_slices import finish, review


async def completed_review_and_daily(db, actor, monkeypatch):
    config, root = await review(db, actor, monkeypatch)
    await finish(db, root)
    end = (datetime.fromisoformat(root.params_json["window_end"]) + timedelta(days=1)).isoformat()
    common = dict(
        tenant_id=actor.tenant_id,
        config_id=config.id,
        config_snapshot=root.config_snapshot,
        max_api_calls=100,
        max_orders=100,
        deadline_at=root.deadline_at,
    )
    second = TransactionRun(
        id=uuid4(),
        **common,
        origin="manual",
        work_key=uuid4().hex,
        params_json={
            **root.params_json,
            "evaluation_key": str(uuid4()),
            "window_start": root.params_json["window_end"],
            "window_end": end,
        },
        progress_json=dict(root.progress_json),
        status="finished",
        termination_reason="done",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        finished_at=datetime.now(timezone.utc),
    )
    scheduled = TransactionRun(
        id=uuid4(),
        **common,
        origin="schedule",
        work_key=uuid4().hex,
        params_json={
            "origin": "schedule",
            "evaluation_key": str(uuid4()),
            "order_references": [],
            "window_start": root.params_json["window_start"],
            "window_end": end,
        },
        progress_json={"schedule_cycle_key": "daily:2026-09-25T07:00:00+00:00"},
        status="pending",
    )
    config.enabled = config.schedule_enabled = True
    db.add_all([second, scheduled])
    return config, root, second, scheduled


def span(run):
    return ReviewSpan(id=run.id, start=run.params_json["window_start"], end=run.params_json["window_end"])


async def test_daily_reuses_complete_custom_slices_without_provider_or_jev_calls(db, admin_user, monkeypatch):
    actor = admin_user[0]
    config, first, second, run = await completed_review_and_daily(db, actor, monkeypatch)
    originals = [(r.finished_at, dict(r.progress_json)) for r in (first, second)]
    provider = AsyncMock(side_effect=AssertionError("Covered daily window must not read providers"))
    from app.services.transaction_ops import hybrid_classification

    classification = AsyncMock(side_effect=AssertionError("No repeated classification"))
    monkeypatch.setattr(hybrid_classification, "classify_report", classification)
    outcome = await runner.run_investigation(
        db,
        actor.tenant_id,
        run.id,
        _source_reader=provider,
        _page_reader=provider,
        _target_reader=provider,
        _source_refunds_reader=provider,
        _target_refunds_reader=provider,
        _refund_page_reader=provider,
        _dependency_page_reader=provider,
        _dependency_owner_reader=provider,
        _enabled=AsyncMock(return_value=True),
    )
    assert outcome["termination_reason"] == "done"
    assert run.api_calls_used == run.orders_used == 0
    assert run.progress_json["processed"] == 0
    assert set(run.progress_json["reused_observation_run_ids"]) == {str(first.id), str(second.id)}
    assert run.progress_json["reused_daily_run_ids"] == []
    assert "dependency_scan_complete" not in run.progress_json
    assert run.progress_json["schedule_cycle_key"].startswith("daily:")
    assert [(r.finished_at, r.progress_json) for r in (first, second)] == originals
    provider.assert_not_awaited()
    classification.assert_not_awaited()
    audits = (
        await db.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == actor.tenant_id, AuditEvent.action == "transaction_ops.run.evidence_reused"
            )
        )
    ).all()
    assert len(audits) == 1 and audits[0].payload["fresh_provider_reads"] == 0
    health = next(r for r in await daily_status.daily_status(db, actor.tenant_id) if r["config_id"] == str(config.id))
    assert health["completed_until"] == datetime.fromisoformat(run.params_json["window_end"]).isoformat()
    assert health["completion_basis"] == "reused_scan_coverage"
    assert health["last_completed_at"] == max(first.finished_at, second.finished_at).isoformat()
    next_scope, resume, reason = scheduler._scope(config, run, datetime.now(timezone.utc))
    assert next_scope["window_end"] == datetime.fromisoformat(run.params_json["window_end"]) + timedelta(days=1)
    assert resume is reason is None
    # Durable done/lease fencing makes duplicate job deliveries no-ops.
    again = await runner.run_investigation(
        db, actor.tenant_id, run.id, _page_reader=provider, _enabled=AsyncMock(return_value=True)
    )
    assert again["status"] != "running"
    provider.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        "gap",
        "basis",
        "mapping",
        "account",
        "entity",
        "connection",
        "contract",
        "discovery",
        "source_incomplete",
        "refund_incomplete",
        "destination_incomplete",
        "dependency_incomplete",
        "seed_incomplete",
        "failed",
        "receipt",
        "exact_order",
        "future",
        "before_window_closed",
        "before_overlap_settled",
        "started_before_overlap_settled",
        "continuation_started_before_overlap_settled",
    ],
)
async def test_daily_coverage_fails_closed_for_incompatible_or_incomplete_evidence(db, admin_user, monkeypatch, change):
    _, first, second, run = await completed_review_and_daily(db, admin_user[0], monkeypatch)
    if change == "gap":
        second.params_json = {
            **second.params_json,
            "window_start": (span(second).start + timedelta(hours=1)).isoformat(),
        }
    elif change == "basis":
        second.params_json = {**second.params_json, "window_basis": "completed_at"}
    elif change == "mapping":
        second.config_snapshot = {
            **second.config_snapshot,
            "mapping_json": {**second.config_snapshot["mapping_json"], "reference_field": "other"},
        }
    elif change in {"account", "entity", "connection", "contract", "discovery"}:
        key, value = {
            "account": ("netsuite_account_id", "other"),
            "entity": ("subsidiary_id", "999"),
            "connection": ("source_connection_id", str(uuid4())),
            "contract": ("evidence_contract_version", 999),
            "discovery": ("destination_discovery_version", 1),
        }[change]
        second.config_snapshot = {**second.config_snapshot, key: value}
    elif change.endswith("_incomplete"):
        key = {
            "source_incomplete": "scan_complete",
            "refund_incomplete": "refund_scan_complete",
            "destination_incomplete": "destination_scan_complete",
            "dependency_incomplete": "dependency_scan_complete",
            "seed_incomplete": "dependency_index_seed",
        }[change]
        second.progress_json = {
            **second.progress_json,
            key: {"complete": False} if key == "dependency_index_seed" else False,
        }
    elif change == "failed":
        second.termination_reason = "error"
    elif change == "receipt":
        second.progress_json = {**second.progress_json, "reused_observation_run_ids": [str(first.id)]}
    elif change == "exact_order":
        second.params_json = {**second.params_json, "order_references": ["R123456789"]}
    elif change == "future":
        second.finished_at = datetime.now(timezone.utc) + timedelta(days=1)
    elif change == "before_window_closed":
        second.finished_at = span(second).end - timedelta(seconds=1)
    else:
        cutoff = daily_evidence.scheduled_observation_floor(run)
        if change == "before_overlap_settled":
            second.created_at, second.finished_at = span(second).end, cutoff - timedelta(seconds=1)
        elif change == "started_before_overlap_settled":
            second.created_at, second.finished_at = cutoff - timedelta(seconds=1), cutoff + timedelta(hours=2)
        else:
            second.progress_json = {
                **second.progress_json,
                "continuation_started_at": (cutoff - timedelta(seconds=1)).isoformat(),
            }
    await db.flush()
    assert await daily_evidence.coverage_receipt(db, run, span(run)) is None


async def test_shared_coverage_cannot_borrow_from_another_tenant_or_configuration(
    db, admin_user, admin_user_b, monkeypatch
):
    _, _, second, run = await completed_review_and_daily(db, admin_user[0], monkeypatch)
    _, foreign, _, foreign_run = await completed_review_and_daily(db, admin_user_b[0], monkeypatch)
    assert (
        await daily_evidence.coverage_receipt(db, run, span(run), source_ids=[str(foreign.id), str(foreign_run.id)])
        is None
    )
    _, other_first, other_second, _ = await completed_review_and_daily(db, admin_user[0], monkeypatch)
    assert (
        await daily_evidence.coverage_receipt(
            db, run, span(run), source_ids=[str(other_first.id), str(other_second.id)]
        )
        is None
    )


async def test_receipt_health_revalidates_original_proof_and_falls_back_to_real_scan(db, admin_user, monkeypatch):
    config, first, second, run = await completed_review_and_daily(db, admin_user[0], monkeypatch)
    older = await daily(db, first, params={"window_end": first.params_json["window_end"]})
    receipt = await daily_evidence.coverage_receipt(db, run, span(run))
    run.progress_json = {**receipt, "reused_observation_run_ids": [str(first.id)]}
    run.status, run.termination_reason, run.finished_at = "finished", "done", datetime.now(timezone.utc)
    # A missing/invalid source cannot turn a saved receipt into independent proof.
    await db.flush()
    health = next(r for r in await daily_status.daily_status(db, run.tenant_id) if r["config_id"] == str(config.id))
    assert health["run_id"] == str(older.id)
    assert health["completion_basis"] == "scan_coverage"
    assert (
        await daily_evidence.coverage_receipt(
            db, run, span(run), source_ids=run.progress_json["reused_observation_run_ids"]
        )
        is None
    )


@pytest.mark.parametrize("mode", ["uncovered", "already_collecting", "resumed_empty_scan"])
async def test_daily_still_reads_uncovered_windows_and_preserves_started_work(db, admin_user, monkeypatch, mode):
    from app.services.transaction_ops import metabase_reader

    actor = admin_user[0]
    _, _, _, run = await completed_review_and_daily(db, actor, monkeypatch)
    if mode == "uncovered":
        run.params_json = {**run.params_json, "window_end": (span(run).end + timedelta(days=1)).isoformat()}
    elif mode == "resumed_empty_scan":
        run.progress_json = {
            **run.progress_json,
            "scan_count": 10,
            "continuation_baseline": {"processed": 0},
            "evidence_root_id": str(uuid4()),
        }
    else:
        run.api_calls_used = 1
        run.progress_json = {**run.progress_json, "processed": 1}
    # A bounded mock failure stops the test at the first real collection step.
    provider = AsyncMock(side_effect=metabase_reader.ReplicaReadError("invalid_binding"))
    monkeypatch.setattr(metabase_reader, "read_order_page", provider)
    await db.flush()
    await runner.run_investigation(db, actor.tenant_id, run.id, _enabled=AsyncMock(return_value=True))
    provider.assert_awaited_once()
    assert "reused_observation_run_ids" not in run.progress_json
    if mode == "already_collecting":
        assert run.progress_json["processed"] == 1
    elif mode == "resumed_empty_scan":
        assert run.progress_json["scan_count"] == 10
    else:
        assert run.params_json["window_end"] == (span(run).start + timedelta(days=3)).isoformat()


@pytest.mark.parametrize(
    "end,cutoff",
    [
        ("2026-03-08T08:00:00+00:00", "2026-03-08T16:00:00+00:00"),
        ("2026-11-01T07:00:00+00:00", "2026-11-01T17:00:00+00:00"),
    ],
)
def test_shared_coverage_preserves_local_settle_hour_across_dst(end, cutoff):
    from types import SimpleNamespace

    root = SimpleNamespace(
        params_json={"window_end": end},
        config_snapshot={
            "mapping_json": {"reconciliation_policy": {"timezone_name": "America/Los_Angeles", "daily_check_hour": 9}}
        },
    )
    assert daily_evidence.scheduled_observation_floor(root) == datetime.fromisoformat(cutoff)
