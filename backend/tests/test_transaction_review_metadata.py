"""Projection parity, database write synchronization and bounded tenant maintenance."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, literal, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.exc import DBAPIError

from app.models.transaction_ops import TransactionFinding
from app.services.transaction_ops.review_evidence import evidence_time, final_evidence
from app.services.transaction_ops.review_metadata import backfill_batch
from app.services.transaction_ops.source_eligibility import eligible_reports
from tests.test_transaction_review_results import evidence
from tests.test_transaction_review_slices import review

REPORT = {
    "source": {"record_id": "123", "observed_at": "2026-09-01T01:00:00Z", "lines": ["large source payload"] * 1000},
    "targets": [{"observed_at": "2026-09-01T02:00:00Z", "lines": ["large ERP payload"] * 1000}],
    "balance": {"status": "difference", "currency": "USD", "amounts": {"tax": {"delta": "0.01"}}},
}


def values(report, updated):
    return [
        evidence_time(report, updated),
        final_evidence(report),
        eligible_reports(report),
        report["source"]["record_id"].astext,
        report["balance"]["currency"].astext,
        report["balance"]["status"].astext,
    ]


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"targets": []},
        {"targets": None},
        {"targets": {}},
        {"targets": [None, {}, {"observed_at": "2026-09-01T02:00:00Z"}]},
        {"targets": [{"observed_at": "not-a-date"}]},
        {"targets": [{"observed_at": "2026-09-02T00:00:01Z"}]},
        {"targets": [{"observed_at": "2026-09-01T00:00:00"}]},
        {"targets": [{"observed_at": ["2026-09-01T00:00:00Z"]}]},
        {"source": {}},
        {"source": None, "balance": None},
        {"source": {"record_id": 123, "observed_at": "2026-08-31T18:00:00-07:00"}},
        {"_observation": {"final": False, "observed_at": "2026-09-01T05:00:00Z"}},
        {"_observation": {"final": "false", "observed_at": None}},
        {"_observation": None, "source_eligibility": {"eligible": False}},
        {"source_eligibility": {"eligible": "false"}},
        {"source_eligibility": {"eligible": "unknown"}},
    ],
)
async def test_projection_preserves_exact_evidence_semantics(db, patch):
    inputs = select(literal({**REPORT, **patch}, type_=JSONB).label("report")).cte("inputs")
    report = inputs.c.report
    projected = func.public.transaction_review_metadata(report, type_=JSONB)
    updated = literal(datetime(2026, 9, 2, tzinfo=timezone.utc))
    row = (await db.execute(select(*values(report, updated), *values(projected, updated)))).one()
    assert row[:6] == row[6:]


async def test_projection_keeps_malformed_top_level_values_and_drops_large_details(db):
    for value in [None, [], "invalid", 123]:
        result = await db.scalar(select(func.public.transaction_review_metadata(literal(value, type_=JSONB))))
        assert result == value
    projected = await db.scalar(select(func.public.transaction_review_metadata(literal(REPORT, type_=JSONB))))
    assert "lines" not in projected["source"] and "lines" not in projected["targets"][0]
    assert "amounts" not in projected["balance"]


async def test_projection_follows_orm_and_bulk_upserts_and_cannot_be_forged(db, admin_user, monkeypatch):
    actor = admin_user[0]
    _, run = await review(db, actor, monkeypatch)
    row = await evidence(db, actor, run, "R123456789", "difference", run.created_at)
    await db.refresh(row)
    assert row.review_metadata_json["balance"]["status"] == "difference"
    row.report_json = deepcopy(REPORT)
    await db.flush()
    await db.refresh(row)
    assert row.review_metadata_json["source"]["record_id"] == "123"
    report = {**REPORT, "_observation": {"observed_at": "2026-09-01T03:00:00Z", "final": False}}
    statement = insert(TransactionFinding).values(
        tenant_id=actor.tenant_id, run_id=run.id, order_reference=row.order_reference, report_json=report
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=["tenant_id", "run_id", "order_reference"],
            set_={"report_json": statement.excluded.report_json},
        )
    )
    await db.execute(update(TransactionFinding).where(TransactionFinding.id == row.id).values(review_metadata_json={}))
    await db.refresh(row)
    assert row.review_metadata_json["_observation"] == report["_observation"]


async def test_backfill_is_tenant_scoped_bounded_and_preserves_report_timestamps(db, admin_user, monkeypatch):
    actor = admin_user[0]
    _, run = await review(db, actor, monkeypatch)
    rows = [await evidence(db, actor, run, f"R{i:09}", "difference", run.created_at) for i in range(3)]
    before = [(r.id, deepcopy(r.report_json), r.updated_at) for r in rows]
    # Simulate rows predating the additive migration, within this rollback-only
    # local test transaction. Normal writers can never set a stale projection.
    await db.execute(text("ALTER TABLE transaction_ops_findings DISABLE TRIGGER sync_transaction_review_metadata"))
    await db.execute(
        text("UPDATE transaction_ops_findings SET review_metadata_json=NULL WHERE tenant_id=:tenant"),
        {"tenant": actor.tenant_id},
    )
    await db.execute(text("ALTER TABLE transaction_ops_findings ENABLE TRIGGER sync_transaction_review_metadata"))
    from uuid import uuid4

    from app.services.transaction_ops.workspace_results import review_page

    original_page = await review_page(db, actor.tenant_id, [run.id])
    assert await backfill_batch(db, uuid4(), limit=2) == {"updated": 0, "remaining": 0}
    assert await backfill_batch(db, actor.tenant_id, limit=2) == {"updated": 2, "remaining": 1}
    assert await backfill_batch(db, actor.tenant_id, limit=2) == {"updated": 1, "remaining": 0}
    assert await backfill_batch(db, actor.tenant_id, limit=2) == {"updated": 0, "remaining": 0}
    assert await review_page(db, actor.tenant_id, [run.id]) == original_page
    for row, (identity, report, updated) in zip(rows, before):
        await db.refresh(row)
        assert (row.id, row.report_json, row.updated_at) == (identity, report, updated)
        assert row.review_metadata_json["balance"]["status"] == "difference"


@pytest.mark.parametrize("field", ["tenant_id", "run_id", "order_reference", "created_at"])
async def test_projection_does_not_weaken_immutable_finding_identity(db, admin_user, monkeypatch, field):
    from uuid import uuid4

    actor = admin_user[0]
    _, run = await review(db, actor, monkeypatch)
    row = await evidence(db, actor, run, "R123456789", "difference", run.created_at)
    value = {
        "tenant_id": uuid4(),
        "run_id": uuid4(),
        "order_reference": "R987654321",
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
    }[field]
    async with db.begin_nested():
        with pytest.raises(DBAPIError, match="immutable transaction evidence"):
            await db.execute(update(TransactionFinding).where(TransactionFinding.id == row.id).values({field: value}))
        await db.rollback()
