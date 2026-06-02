from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.reconciliation import ReconciliationResult
from app.services.reconciliation.four_bucket_classifier import (
    ALL_BUCKETS,
    bucket_conditions,
    classify,
)
from tests.conftest import create_test_recon_result, create_test_recon_run


async def test_factories_seed_run_and_result(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id)
    res = await create_test_recon_result(db, tenant_a.id, run.id, match_type="fuzzy")
    await db.flush()
    assert res.run_id == run.id
    assert res.tenant_id == tenant_a.id
    assert res.status == "suggested"


# (match_type, variance_type, variance_amount)
_MATRIX = [
    ("deterministic", None, Decimal("0")),
    ("deterministic", "amount_mismatch", Decimal("0.12")),
    ("deterministic", None, Decimal("5.00")),
    ("fuzzy", None, Decimal("0")),
    ("fuzzy", "amount_mismatch", Decimal("5.11")),
    ("unmatched", "missing_in_netsuite", Decimal("1203.68")),
    ("exception", "duplicate", Decimal("0")),
    ("unmatched", "missing", Decimal("0")),
]


async def test_sql_twin_partitions_identically_to_classify(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id)
    seeded = []
    for mt, vt, va in _MATRIX:
        r = await create_test_recon_result(db, tenant_a.id, run.id, match_type=mt, variance_type=vt, variance_amount=va)
        seeded.append(r)
    await db.flush()

    # Every seeded row appears in exactly one bucket via the SQL twin, and that
    # bucket equals classify() for the same row.
    seen: dict = {}
    for bucket in ALL_BUCKETS:
        rows = (
            (
                await db.execute(
                    select(ReconciliationResult.id).where(
                        ReconciliationResult.run_id == run.id,
                        bucket_conditions(bucket),
                    )
                )
            )
            .scalars()
            .all()
        )
        for rid in rows:
            assert rid not in seen, "row matched two buckets"
            seen[rid] = bucket

    assert len(seen) == len(seeded), "row matched zero buckets"
    for r in seeded:
        py_bucket = classify(r.match_type, r.variance_type, r.variance_amount)
        assert seen[r.id] == py_bucket


def test_bucket_conditions_rejects_unknown_bucket():
    with pytest.raises(ValueError):
        bucket_conditions("not_a_bucket")
