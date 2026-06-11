"""DB-backed selection tests for the chat MCP tool ``recon.get_exceptions`` (Task A).

Row-level proof of the bucket-keyed selection re-key against real Postgres via
the conftest ``db`` fixture (each test rolled back): exceptions = the
authoritative ``needs_review`` bucket, excluding already-dispositioned
(approved/locked) rows; ``min_variance`` is a Decimal-safe abs filter;
ordering is largest ABSOLUTE variance first; ``exception_count`` is the TRUE
filtered total (with ``returned``/``truncated``); zero amounts serialize as
"0.00", not null. The compiled-SQL/unit twin is
``test_recon_exceptions_tool.py`` (the >50-row truncation case lives there).

Written rigorously following the existing recon DB-test patterns but NOT run in
the implementer environment (no DB here); the PM runs them post-flight.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.mcp.tools.recon_exceptions import execute
from app.services.reconciliation.four_bucket_classifier import (
    BUCKET_NEEDS_REVIEW,
    BUCKET_RULES,
)
from tests.conftest import create_test_recon_result, create_test_recon_run


async def test_selection_is_bucket_keyed_excluding_dispositioned(db, tenant_a):
    """auto_matched+needs_review IS an exception; suggested+rules is NOT;
    approved/locked needs_review rows are already dispositioned → excluded."""
    run = await create_test_recon_run(db, tenant_a.id)

    # Material matched-variance row left for review by the close lock-matrix:
    # the structurally-live combo the old status-keyed selection missed.
    visible_auto = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="auto_matched",
        match_type="deterministic",
        variance_type="fees",
        variance_amount=Decimal("75.00"),
        netsuite_amount=Decimal("925.00"),
        stripe_amount=Decimal("1000.00"),
        bucket=BUCKET_NEEDS_REVIEW,
    )
    # Plain unmatched pending row — classify() puts it in needs_review.
    visible_unmatched = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        match_type="unmatched",
        match_rule=None,
        variance_type="missing",
        variance_amount=Decimal("100.00"),
        netsuite_amount=None,
        stripe_amount=Decimal("100.00"),
    )
    # suggested + rules bucket: open, but NOT in the needs_review bucket.
    rules_row = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="suggested",
        match_type="fuzzy",
        variance_type="fx_rounding",
        variance_amount=Decimal("0.30"),
    )
    assert rules_row.bucket == BUCKET_RULES  # factory classify() sanity
    # Already-dispositioned needs_review rows: not OPEN exceptions.
    approved_row = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="approved",
        match_type="unmatched",
        variance_type="missing",
        variance_amount=Decimal("50.00"),
    )
    locked_row = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="locked",
        match_type="unmatched",
        variance_type="missing",
        variance_amount=Decimal("60.00"),
    )
    await db.flush()

    out = await execute({"run_id": str(run.id)}, db=db, tenant_id=tenant_a.id)

    assert out["success"] is True
    returned_ids = {e["result_id"] for e in out["exceptions"]}
    assert str(visible_auto.id) in returned_ids
    assert str(visible_unmatched.id) in returned_ids
    assert str(rules_row.id) not in returned_ids
    assert str(approved_row.id) not in returned_ids
    assert str(locked_row.id) not in returned_ids
    assert out["exception_count"] == 2


async def test_payload_reframe_and_orm_evidence_not_mutated(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id)
    row = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="auto_matched",
        match_type="deterministic",
        confidence=Decimal("0.4200"),
        variance_type="fees",
        variance_amount=Decimal("75.00"),
        bucket=BUCKET_NEEDS_REVIEW,
    )
    row.evidence = {
        "order_reference": "R123456789",
        "confidence_signals": {"amount_score": "0.9", "temporal_score": None, "composite": "0.54"},
    }
    await db.flush()

    out = await execute({"run_id": str(run.id)}, db=db, tenant_id=tenant_a.id)

    assert out["success"] is True
    exc = out["exceptions"][0]
    # Advisory rename + authoritative disposition fields.
    assert exc["advisory_match_score"] == "0.4200"
    assert "confidence" not in exc
    assert exc["status"] == "auto_matched"
    assert exc["bucket"] == BUCKET_NEEDS_REVIEW
    # confidence_signals stripped from the payload via a COPY...
    assert "confidence_signals" not in exc["evidence"]
    assert exc["evidence"]["order_reference"] == "R123456789"
    # ...while the stored ORM evidence is NOT mutated.
    assert "confidence_signals" in row.evidence


async def test_min_variance_filters_decimal_safe_abs(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id)
    big = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        match_type="unmatched",
        variance_type="missing",
        variance_amount=Decimal("120.00"),
    )
    # Negative variance with |v| above the threshold must still be included (abs filter).
    negative_big = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        match_type="unmatched",
        variance_type="missing",
        variance_amount=Decimal("-80.00"),
    )
    small = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        match_type="unmatched",
        variance_type="missing",
        variance_amount=Decimal("10.00"),
    )
    await db.flush()

    out = await execute({"run_id": str(run.id), "min_variance": "50.00"}, db=db, tenant_id=tenant_a.id)

    assert out["success"] is True
    returned_ids = {e["result_id"] for e in out["exceptions"]}
    assert str(big.id) in returned_ids
    assert str(negative_big.id) in returned_ids
    assert str(small.id) not in returned_ids


async def test_ordering_largest_absolute_variance_first_with_true_total(db, tenant_a):
    """Signed desc would sort the -120.00 refund-heavy row dead-last (and at
    scale truncate it below the 50-row cap); abs desc surfaces it FIRST.
    Payload also carries the TRUE total + returned + truncated."""
    run = await create_test_recon_run(db, tenant_a.id)
    positive_mid = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        match_type="unmatched",
        variance_type="missing",
        variance_amount=Decimal("50.00"),
    )
    negative_biggest = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        match_type="unmatched",
        variance_type="missing",
        variance_amount=Decimal("-120.00"),
    )
    positive_small = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        match_type="unmatched",
        variance_type="missing",
        variance_amount=Decimal("30.00"),
    )
    await db.flush()

    out = await execute({"run_id": str(run.id)}, db=db, tenant_id=tenant_a.id)

    assert out["success"] is True
    assert [e["result_id"] for e in out["exceptions"]] == [
        str(negative_biggest.id),
        str(positive_mid.id),
        str(positive_small.id),
    ]
    # Count honesty fields (no truncation at 3 rows; >50 proof in the unit twin).
    assert out["exception_count"] == 3
    assert out["returned"] == 3
    assert out["truncated"] is False


async def test_zero_amounts_serialize_as_zero_not_null(db, tenant_a):
    """A genuine Decimal("0.00") amount is falsy — it must come back as
    "0.00", never null (the old truthiness check erased it)."""
    run = await create_test_recon_run(db, tenant_a.id)
    await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        match_type="unmatched",
        variance_type="missing",
        variance_amount=Decimal("100.00"),
        stripe_amount=Decimal("0.00"),
        netsuite_amount=Decimal("0.00"),
    )
    await db.flush()

    out = await execute({"run_id": str(run.id)}, db=db, tenant_id=tenant_a.id)

    exc = out["exceptions"][0]
    assert exc["stripe_amount"] == "0.00"
    assert exc["netsuite_amount"] == "0.00"


async def test_other_tenant_and_other_run_rows_invisible(db, tenant_a, tenant_b):
    run_a = await create_test_recon_run(db, tenant_a.id)
    run_b = await create_test_recon_run(db, tenant_b.id)
    mine = await create_test_recon_result(
        db,
        tenant_a.id,
        run_a.id,
        status="pending",
        match_type="unmatched",
        variance_type="missing",
        variance_amount=Decimal("10.00"),
    )
    # Foreign row differing in BOTH tenant and run — excluded by either filter.
    await create_test_recon_result(
        db,
        tenant_b.id,
        run_b.id,
        status="pending",
        match_type="unmatched",
        variance_type="missing",
        variance_amount=Decimal("10.00"),
    )
    # Cross-tenant row on the SAME run_a: only the tenant_id where-clause can
    # exclude it (the run_id predicate matches), so this row is what actually
    # proves tenant scoping. ReconciliationResult.tenant_id is a plain column —
    # seedable — and the conftest ``db`` session connects as table owner, so
    # RLS does NOT backstop the in-query filter here. This regression class is
    # live in this repo: commit 34b8f50 fixed exactly a missing tenant filter
    # in the evidence-download query.
    cross_tenant_same_run = await create_test_recon_result(
        db,
        tenant_b.id,
        run_a.id,
        status="pending",
        match_type="unmatched",
        variance_type="missing",
        variance_amount=Decimal("10.00"),
    )
    await db.flush()

    out = await execute({"run_id": str(run_a.id)}, db=db, tenant_id=tenant_a.id)

    assert out["success"] is True
    returned_ids = {e["result_id"] for e in out["exceptions"]}
    assert str(cross_tenant_same_run.id) not in returned_ids  # tenant filter, not run filter
    assert returned_ids == {str(mine.id)}

    out_wrong_run = await execute({"run_id": str(uuid.uuid4())}, db=db, tenant_id=tenant_a.id)
    assert out_wrong_run["success"] is True
    assert out_wrong_run["exception_count"] == 0
