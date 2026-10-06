from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.encryption import encrypt_credentials
from app.models.connection import Connection
from app.models.transaction_evidence_batch import TransactionEvidenceBatch as Batch
from app.models.transaction_ops import TransactionRun
from app.schemas.transaction_runs import ReviewSpan, RunCreate
from app.services.transaction_ops import daily_evidence, period_review, runner
from app.services.transaction_ops.netsuite_change_owners import candidate_membership, collect_order_candidates
from app.services.transaction_ops.period_membership import KEY, Membership
from tests.test_netsuite_change_owners import Reader, edge, record
from tests.test_transaction_period_review_api import ready
from tests.test_transaction_review_results import evidence


async def setup(db, admin_user, monkeypatch, *, empty=False, failure=None):
    actor = admin_user[0]
    config = await ready(db, actor, monkeypatch)
    root = await period_review.create_review(
        db,
        actor.tenant_id,
        config.id,
        period_review.PeriodReview(
            evaluation_key=uuid4(),
            evidence_mode="saved",
            period="custom",
            start_date="2026-08-02",
            end_date="2026-08-02",
        ),
        actor=actor,
    )
    whole = SimpleNamespace(start=ReviewSpan.model_validate(root.params_json["review"]).start - timedelta(days=1))
    row = TransactionRun(
        tenant_id=actor.tenant_id,
        config_id=config.id,
        origin="schedule",
        work_key=uuid4().hex,
        config_snapshot=root.config_snapshot,
        params_json=RunCreate(
            origin="schedule",
            evaluation_key=uuid4().hex,
            window_start=whole.start,
            window_end=whole.start + timedelta(days=2),
            window_basis="updated_at",
        ).model_dump(mode="json", exclude={"review"}),
        progress_json={},
        status="pending",
        max_api_calls=100,
        max_orders=100,
        deadline_at=root.deadline_at,
    )
    db.add(row)
    await db.flush()
    connection = await db.scalar(select(Connection).where(Connection.id == config.netsuite_connection_id))
    connection.encrypted_credentials = encrypt_credentials(
        {"account_id": config.netsuite_account_id, "access_token": "test"}
    )
    progress = {}
    store = Membership(db, actor.tenant_id, row, progress, lambda: datetime.now(timezone.utc))
    if not empty:
        # The final source snapshot may be newer than the selected day. Membership
        # comes from original discovery, not those final snapshots.
        store.capture(["R123456789"], (whole.start + timedelta(hours=2)).isoformat())
        store.capture(["R123456788"], (whole.start + timedelta(days=1, hours=2)).isoformat())
        await evidence(db, actor, row, "R123456789", "difference", row.created_at)
        await evidence(db, actor, row, "R123456788", "matched", row.created_at)
    progress.update(
        scan_complete=True,
        refund_scan_complete=True,
        destination_scan_complete=True,
        dependency_scan_complete=True,
        dependency_index_seed={"complete": True},
    )
    if failure == "legacy":
        progress.pop(KEY)
    elif failure == "unsupported":
        progress[KEY]["supported"] = False
    elif failure == "interim":
        progress["dependency_scan_complete"] = False
    elif failure == "receipt":
        progress["reused_observation_run_ids"] = []
    await store.seal()
    row.progress_json = deepcopy(progress)
    row.status, row.termination_reason, row.finished_at = "finished", "done", datetime.now(timezone.utc)
    await db.flush()
    return actor, root, row, store


async def test_saved_single_day_reuses_wider_scan_with_exact_membership_and_no_provider_calls(
    db, admin_user, monkeypatch
):
    actor, root, row, _ = await setup(db, admin_user, monkeypatch)
    results = await period_review.review_results(db, actor.tenant_id, root.id)
    assert results["summary"] == {"checked": 1, "matched": 1, "needs_review": 0, "not_verified": 0}
    assert [item["order_reference"] for item in results["items"]] == ["R123456788"]
    receipt = await daily_evidence.coverage_receipt(
        db, root, ReviewSpan.model_validate(root.params_json["review"]), whole_span=True
    )
    assert receipt["reused_observation_run_ids"] == [str(row.id)]
    providers = AsyncMock(side_effect=AssertionError("saved day must not read providers"))
    result = await runner.run_investigation(
        db,
        actor.tenant_id,
        root.id,
        _source_reader=providers,
        _target_reader=providers,
        _page_reader=providers,
        _enabled=AsyncMock(return_value=True),
    )
    assert result["termination_reason"] == "done" and root.api_calls_used == 0
    providers.assert_not_awaited()


async def test_proven_empty_day_is_complete_without_inventing_matches(db, admin_user, monkeypatch):
    actor, root, _, _ = await setup(db, admin_user, monkeypatch, empty=True)
    results = await period_review.review_results(db, actor.tenant_id, root.id)
    assert results["summary"]["checked"] == 0
    assert (await period_review.review_status(db, actor.tenant_id, root.id))["complete"]


@pytest.mark.parametrize(
    "change", ["legacy", "unsupported", "interim", "receipt", "mapping", "missing_page", "current"]
)
async def test_incomplete_or_incompatible_membership_cannot_certify_smaller_period(db, admin_user, monkeypatch, change):
    actor, root, row, _ = await setup(db, admin_user, monkeypatch, failure=change)
    if change == "mapping":
        root = SimpleNamespace(
            id=root.id,
            config_id=root.config_id,
            tenant_id=root.tenant_id,
            origin=root.origin,
            params_json=root.params_json,
            config_snapshot={
                **root.config_snapshot,
                "mapping_json": {**root.config_snapshot["mapping_json"], "reference_field": "other"},
            },
        )
    elif change == "current":
        root = SimpleNamespace(
            id=root.id,
            config_id=root.config_id,
            tenant_id=root.tenant_id,
            origin=root.origin,
            params_json={**root.params_json, "evidence_mode": "current"},
            config_snapshot=root.config_snapshot,
        )
    elif change == "missing_page":
        pages = list(await db.scalars(select(Batch).where(Batch.run_id == row.id, Batch.id != row.id)))
        await db.delete(pages[0])
    await db.flush()
    receipt = await daily_evidence.coverage_receipt(
        db, root, ReviewSpan.model_validate(root.params_json["review"]), whole_span=True
    )
    assert receipt is None


def test_existing_checkpoint_is_not_backfilled_from_final_snapshots():
    run = SimpleNamespace(
        id=uuid4(),
        config_snapshot={"mapping_json": {"metabase_replica": True}, "destination_discovery_version": 2},
        params_json={"window_start": "2026-10-03T07:00:00Z", "window_end": "2026-10-05T07:00:00Z"},
        progress_json={"scan_count": 20, "last_source_id": 99},
    )
    assert not Membership(object(), uuid4(), run, dict(run.progress_json), lambda: None).active


async def test_refund_membership_uses_refund_date_and_keeps_original_clock(db, admin_user, monkeypatch):
    actor, root, row, store = await setup(db, admin_user, monkeypatch, empty=True)
    # A new discovery continuation would retain the same compact root/context.
    later = deepcopy(store.progress)
    run = SimpleNamespace(
        id=uuid4(), progress_json=later, config_snapshot=row.config_snapshot, params_json=row.params_json
    )
    resumed = Membership(db, actor.tenant_id, run, later, store.clock)
    assert resumed.active and later[KEY] == store.progress[KEY]
    start = ReviewSpan.model_validate(root.params_json["review"]).start
    resumed.refunds({"refunds": [{"order_reference": "R123456788", "updated_at": start.isoformat()}]}, ["R123456788"])
    assert resumed.pending == [{"reference": "R123456788", "at": start.isoformat()}]
    resumed.capture(["R123456789"], "not-a-time")
    assert not resumed.active and not resumed.pending


async def test_native_membership_keeps_ambiguous_custom_and_reverse_owners_without_extra_calls():
    reader = Reader()
    reader.limit = 1000
    reader.records += [record(8, "SalesOrd"), record(9, "SalesOrd", "3")]
    reader.requests = [{"id": "20", "order_id": "8", "credit_id": "3"}, {"id": "21", "order_id": "9", "credit_id": "3"}]
    result = await collect_order_candidates(reader.request, "2", "custbody_fw_order_number", ["4"], [], [], bulk=True)
    changes = [{"record_keys": [["transaction", "4"]], "modified_at": "2026-10-04T12:00:00Z"}]
    associations = candidate_membership(changes, result["inventory"], "2")
    assert associations[0][1] == result["order_references"] == ["R000000001", "R000000008"]
    assert len(reader.calls) == 6


def test_native_membership_does_not_cross_nomination_dates_or_subsidiaries():
    changes = [{"record_keys": [["transaction", "2"]]}, {"record_keys": [["transaction", "7"]]}]
    inventory = [
        [record(1, "SalesOrd"), record(8, "SalesOrd"), record(9, "SalesOrd", "3")],
        [edge(1, 2, "SalesOrd", "CustDep"), edge(8, 7, "SalesOrd", "CustDep"), edge(9, 7, "SalesOrd", "CustDep")],
    ]
    assert [refs for _, refs in candidate_membership(changes, inventory, "2")] == [["R000000001"], ["R000000008"]]


@pytest.mark.parametrize(
    "start,end,hours",
    [
        ("2026-03-08T08:00:00Z", "2026-03-10T07:00:00Z", [23, 24]),
        ("2026-11-01T07:00:00Z", "2026-11-03T08:00:00Z", [25, 24]),
    ],
)
def test_membership_calendar_bins_preserve_dst_and_missing_aggregate_proof_falls_back(start, end, hours):
    run = SimpleNamespace(
        id=uuid4(),
        progress_json={},
        params_json={"window_start": start, "window_end": end},
        config_snapshot={
            "mapping_json": {
                "metabase_replica": True,
                "reconciliation_policy": {"timezone_name": "America/Los_Angeles"},
            },
            "destination_discovery_version": 2,
        },
    )
    store = Membership(object(), uuid4(), run, {}, lambda: None)
    assert [(upper - lower).total_seconds() / 3600 for lower, upper in store.windows()] == hours
    store.change(["R123456789"], {"modified_at": start}, "transaction_lines")
    assert not store.active  # MAX(modified) cannot prove every day of activity.


async def test_collector_seals_empty_provider_scan_in_its_actual_leased_checkpoint(db, admin_user, monkeypatch):
    from app.services.transaction_ops import metabase_reader

    actor = admin_user[0]
    config = await ready(db, actor, monkeypatch)
    config.max_api_calls = 100
    connection = await db.scalar(select(Connection).where(Connection.id == config.netsuite_connection_id))
    connection.encrypted_credentials = encrypt_credentials(
        {"account_id": config.netsuite_account_id, "access_token": "test"}
    )
    await db.flush()
    root = await period_review.create_review(
        db,
        actor.tenant_id,
        config.id,
        period_review.PeriodReview(
            evaluation_key=uuid4(),
            period="custom",
            evidence_mode="saved",
            start_date="2026-08-02",
            end_date="2026-08-02",
        ),
        actor=actor,
    )
    monkeypatch.setattr(
        metabase_reader,
        "read_order_page",
        AsyncMock(return_value={"orders": [], "page_complete": True, "scan_complete": True, "next_after_id": None}),
    )
    monkeypatch.setattr(
        metabase_reader,
        "read_changed_refund_orders",
        AsyncMock(return_value={"orders": [], "refunds": [], "page_complete": True, "next_after_id": None}),
    )

    async def changes(*args, **kwargs):
        return {
            "stream": args[6],
            "changes": [],
            "page_complete": True,
            "scan_complete": True,
            "next_cursor": None,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "scope": {"window_end": root.params_json["window_end"]},
        }

    financial_read = AsyncMock(side_effect=AssertionError("An empty scan cannot read or write financial records"))
    result = await runner.run_investigation(
        db,
        actor.tenant_id,
        root.id,
        _enabled=AsyncMock(return_value=True),
        _source_reader=financial_read,
        _target_reader=financial_read,
        _dependency_page_reader=changes,
        _dependency_seed=AsyncMock(return_value={"complete": True}),
    )
    assert result["termination_reason"] == "done"
    marker = await db.scalar(select(Batch).where(Batch.id == root.id, Batch.tenant_id == actor.tenant_id))
    assert marker.evidence_json["value"]["complete"] is True
    assert marker.evidence_json["value"]["batches"] == 0
    assert daily_evidence.scan_complete(root)
    financial_read.assert_not_awaited()


async def test_identical_source_and_refund_nominations_do_not_overcount_immutable_pages(db, admin_user, monkeypatch):
    actor = admin_user[0]
    config = await ready(db, actor, monkeypatch)
    connection = await db.scalar(select(Connection).where(Connection.id == config.netsuite_connection_id))
    connection.encrypted_credentials = encrypt_credentials(
        {"account_id": config.netsuite_account_id, "access_token": "test"}
    )
    await db.flush()
    root = await period_review.create_review(
        db,
        actor.tenant_id,
        config.id,
        period_review.PeriodReview(evaluation_key=uuid4(), period="yesterday", evidence_mode="saved"),
        actor=actor,
    )
    progress = {}
    store = Membership(db, actor.tenant_id, root, progress, lambda: datetime.now(timezone.utc))
    for _ in range(2):
        store.capture(["R123456789"], root.params_json["window_start"])
        await store.flush()
    pages = list(await db.scalars(select(Batch).where(Batch.tenant_id == actor.tenant_id, Batch.run_id == root.id)))
    assert len(pages) == progress[KEY]["batches"] == 1
