"""DB-backed schema + backfill tests for migration 078_recon_buckets_materiality.

R2a Task T2. These run against the local docker Postgres via the conftest ``db``
fixture (each test is rolled back). They assert:

  1. The new columns exist with the expected NOT NULL + server_default semantics:
     - reconciliation_results.bucket   VARCHAR(50) NOT NULL DEFAULT 'needs_review'
     - reconciliation_runs.{matches,rules,auto_classifications,needs_review}_count
       INTEGER NOT NULL DEFAULT 0
     - tenant_configs.recon_materiality_abs  NUMERIC(15,2) NOT NULL DEFAULT 50
     - tenant_configs.recon_materiality_pct  NUMERIC(6,4)  NOT NULL DEFAULT 0.0100
  2. The composite index (run_id, bucket) exists on reconciliation_results.
  3. The backfill CASE (default materiality $50 / 0.0100) classifies a seeded
     matrix the same way ``classify()`` does with those thresholds — material
     det/fuzzy variance -> needs_review, immaterial -> auto_classifications/rules,
     deterministic+no-variance -> matches, unmatched -> needs_review.

These are written rigorously following the existing recon DB-test patterns but are
NOT run in the implementer environment (no DB here); the PM runs them post-flight.
"""

import uuid
from decimal import Decimal

from sqlalchemy import select, text

from app.models.reconciliation import ReconciliationResult, ReconciliationRun
from app.models.tenant import TenantConfig
from app.services.reconciliation.four_bucket_classifier import (
    BUCKET_AUTO_CLASSIFICATIONS,
    BUCKET_MATCHES,
    BUCKET_NEEDS_REVIEW,
    BUCKET_RULES,
    classify,
)
from tests.conftest import create_test_recon_result, create_test_recon_run

# Default materiality thresholds baked into migration 078 backfill + column defaults.
_MAT_ABS = Decimal("50")
_MAT_PCT = Decimal("0.0100")


# ---------------------------------------------------------------------------
# 1. Column existence + NOT NULL + server_default semantics
# ---------------------------------------------------------------------------


async def test_reconciliation_results_bucket_column_default(db, tenant_a):
    """bucket is NOT NULL and defaults to 'needs_review' at the DB level."""
    run = await create_test_recon_run(db, tenant_a.id)
    # Insert a row WITHOUT specifying bucket -> server_default must apply.
    await db.execute(
        text(
            "INSERT INTO reconciliation_results "
            "(id, tenant_id, run_id, match_type, confidence, status, variance_amount, currency) "
            "VALUES (:id, :tid, :rid, 'fuzzy', 0, 'pending', 0, 'USD')"
        ),
        {"id": uuid.uuid4(), "tid": tenant_a.id, "rid": run.id},
    )
    await db.flush()
    bucket = (
        await db.execute(
            text("SELECT bucket FROM reconciliation_results WHERE run_id = :rid ORDER BY created_at DESC LIMIT 1"),
            {"rid": run.id},
        )
    ).scalar_one()
    assert bucket == BUCKET_NEEDS_REVIEW


async def test_reconciliation_runs_rollup_count_defaults(db, tenant_a):
    """The 4 per-bucket count columns exist, default to 0, and are NOT NULL."""
    run = await create_test_recon_run(db, tenant_a.id)
    await db.refresh(run)
    assert run.matches_count == 0
    assert run.rules_count == 0
    assert run.auto_classifications_count == 0
    assert run.needs_review_count == 0


async def test_tenant_config_materiality_defaults(db, tenant_a):
    """Materiality threshold columns default to $50 / 0.0100 and are NOT NULL."""
    cfg = (await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_a.id))).scalar_one()
    assert cfg.recon_materiality_abs == _MAT_ABS
    assert cfg.recon_materiality_pct == _MAT_PCT


async def test_reconciliation_results_run_bucket_index_exists(db):
    """A composite index on (run_id, bucket) exists for reconciliation_results."""
    rows = (
        (await db.execute(text("SELECT indexdef FROM pg_indexes WHERE tablename = 'reconciliation_results'")))
        .scalars()
        .all()
    )
    joined = " ".join(rows).lower()
    # Index covers run_id then bucket (order matters for the bucket-filter query).
    assert any("run_id" in d.lower() and "bucket" in d.lower() for d in rows), (
        f"no (run_id, bucket) index found among: {joined}"
    )


# ---------------------------------------------------------------------------
# 2. Backfill CASE correctness — mirrors classify() with default thresholds
# ---------------------------------------------------------------------------

# (match_type, variance_type, variance_amount, stripe_amount, expected_bucket)
# Covers: deterministic no-variance -> matches; immaterial det/fuzzy variance ->
# auto_classifications/rules; material (abs) det/fuzzy -> needs_review; material
# (pct) det -> needs_review; unmatched/exception -> needs_review.
_BACKFILL_MATRIX = [
    # deterministic, no variance -> matches
    ("deterministic", None, Decimal("0"), Decimal("100.00"), BUCKET_MATCHES),
    # deterministic, tiny immaterial variance -> auto_classifications
    ("deterministic", "amount_mismatch", Decimal("0.12"), Decimal("100.00"), BUCKET_AUTO_CLASSIFICATIONS),
    # deterministic, variance below $50 and below 1% -> auto_classifications
    ("deterministic", None, Decimal("5.00"), Decimal("1000.00"), BUCKET_AUTO_CLASSIFICATIONS),
    # deterministic, variance > $50 abs -> needs_review (material by abs)
    ("deterministic", "amount_mismatch", Decimal("60.00"), Decimal("100000.00"), BUCKET_NEEDS_REVIEW),
    # deterministic, variance < $50 but > 1% relative -> needs_review (material by pct)
    ("deterministic", "amount_mismatch", Decimal("2.00"), Decimal("100.00"), BUCKET_NEEDS_REVIEW),
    # fuzzy, no variance -> rules
    ("fuzzy", None, Decimal("0"), Decimal("50.00"), BUCKET_RULES),
    # fuzzy, immaterial variance -> rules
    ("fuzzy", "amount_mismatch", Decimal("0.40"), Decimal("100.00"), BUCKET_RULES),
    # fuzzy, material by abs -> needs_review
    ("fuzzy", "amount_mismatch", Decimal("75.00"), Decimal("100000.00"), BUCKET_NEEDS_REVIEW),
    # fuzzy, material by pct -> needs_review
    ("fuzzy", "amount_mismatch", Decimal("3.00"), Decimal("100.00"), BUCKET_NEEDS_REVIEW),
    # unmatched -> needs_review
    ("unmatched", "missing_in_netsuite", Decimal("1203.68"), None, BUCKET_NEEDS_REVIEW),
    # exception (payout dup) -> needs_review
    ("exception", "duplicate", Decimal("0"), Decimal("10.00"), BUCKET_NEEDS_REVIEW),
]


async def test_backfill_case_matches_classify(db, tenant_a):
    """Re-running the migration's backfill CASE reproduces classify() exactly.

    We seed rows (which under migration 078 already carry a server_default
    bucket), deliberately set ``bucket`` to a wrong sentinel, then apply the
    SAME CASE SQL the migration's upgrade() uses. Each row must land on the
    bucket that classify() returns with the default thresholds.
    """
    run = await create_test_recon_run(db, tenant_a.id)
    seeded: list[tuple[ReconciliationResult, str]] = []
    for mt, vt, va, sa_amt, expected in _BACKFILL_MATRIX:
        r = await create_test_recon_result(
            db,
            tenant_a.id,
            run.id,
            match_type=mt,
            variance_type=vt,
            variance_amount=va,
            stripe_amount=sa_amt,
        )
        seeded.append((r, expected))
    await db.flush()

    # Force a wrong value so the backfill has to overwrite it.
    await db.execute(
        text("UPDATE reconciliation_results SET bucket = 'matches' WHERE run_id = :rid"),
        {"rid": run.id},
    )

    # Exact CASE from migration 078 upgrade() (default materiality 50 / 0.0100).
    backfill_sql = text(
        """
        UPDATE reconciliation_results SET bucket = CASE
          WHEN match_type='deterministic' AND variance_type IS NULL AND variance_amount=0 THEN 'matches'
          WHEN match_type IN ('deterministic','fuzzy')
               AND (variance_type IS NOT NULL OR variance_amount<>0)
               AND ( ABS(variance_amount) > 50
                     OR (stripe_amount IS NOT NULL AND ABS(stripe_amount) > 0
                         AND ABS(variance_amount)/ABS(stripe_amount) > 0.0100) )
            THEN 'needs_review'
          WHEN match_type='deterministic' THEN 'auto_classifications'
          WHEN match_type='fuzzy' THEN 'rules'
          ELSE 'needs_review' END
        WHERE run_id = :rid
        """
    )
    await db.execute(backfill_sql, {"rid": run.id})
    await db.flush()

    for r, expected in seeded:
        await db.refresh(r)
        # 1. backfill landed on the expected bucket
        assert r.bucket == expected, (
            f"backfill bucket {r.bucket!r} != expected {expected!r} "
            f"for ({r.match_type}, {r.variance_type}, {r.variance_amount}, {r.stripe_amount})"
        )
        # 2. backfill agrees with classify() under the same thresholds (single source of truth)
        py_bucket = classify(
            r.match_type,
            r.variance_type,
            r.variance_amount,
            materiality_abs=_MAT_ABS,
            materiality_pct=_MAT_PCT,
            matched_amount=r.stripe_amount,
        )
        assert r.bucket == py_bucket, (
            f"backfill {r.bucket!r} != classify() {py_bucket!r} "
            f"for ({r.match_type}, {r.variance_type}, {r.variance_amount}, {r.stripe_amount})"
        )


# ---------------------------------------------------------------------------
# 2b. Backfill boundary correctness — the literal migration-078 CASE must agree
#     with classify() on the strict-inequality + null/zero/negative edges.
# ---------------------------------------------------------------------------

# The literal backfill UPDATE...CASE copied verbatim from migration 078 upgrade(),
# parameterized only by run_id so the test can scope its rows.
_MIGRATION_078_BACKFILL_SQL = text(
    """
    UPDATE reconciliation_results SET bucket = CASE
      WHEN match_type='deterministic' AND variance_type IS NULL AND variance_amount=0 THEN 'matches'
      WHEN match_type IN ('deterministic','fuzzy')
           AND (variance_type IS NOT NULL OR variance_amount<>0)
           AND ( ABS(variance_amount) > 50
                 OR (stripe_amount IS NOT NULL AND ABS(stripe_amount) > 0
                     AND ABS(variance_amount)/ABS(stripe_amount) > 0.0100) )
        THEN 'needs_review'
      WHEN match_type='deterministic' THEN 'auto_classifications'
      WHEN match_type='fuzzy' THEN 'rules'
      ELSE 'needs_review' END
    WHERE run_id = :rid
    """
)

# (match_type, variance_type, variance_amount, stripe_amount, expected_bucket)
# Boundary-heavy matrix exercising the STRICT inequalities + null/zero/negative
# branches of the migration-078 CASE. Each expected value is independently
# re-derived by classify() below (single source of truth) so a drift in either
# the SQL or the classifier surfaces here.
_BACKFILL_BOUNDARY_MATRIX = [
    # abs variance EXACTLY 50 -> not material (strict >) -> auto_classifications
    ("deterministic", "amount_mismatch", Decimal("50.00"), Decimal("100000.00"), BUCKET_AUTO_CLASSIFICATIONS),
    # abs variance 50.01 -> material by abs (just over) -> needs_review
    ("deterministic", "amount_mismatch", Decimal("50.01"), Decimal("100000.00"), BUCKET_NEEDS_REVIEW),
    # fuzzy, abs EXACTLY 50 -> not material -> rules
    ("fuzzy", "amount_mismatch", Decimal("50.00"), Decimal("100000.00"), BUCKET_RULES),
    # fuzzy, abs 50.01 -> material by abs -> needs_review
    ("fuzzy", "amount_mismatch", Decimal("50.01"), Decimal("100000.00"), BUCKET_NEEDS_REVIEW),
    # pct EXACTLY 1% (1.00 / 100.00) -> not material (strict >) -> auto_classifications
    ("deterministic", "amount_mismatch", Decimal("1.00"), Decimal("100.00"), BUCKET_AUTO_CLASSIFICATIONS),
    # pct 1.01% (1.01 / 100.00) -> material by pct -> needs_review
    ("deterministic", "amount_mismatch", Decimal("1.01"), Decimal("100.00"), BUCKET_NEEDS_REVIEW),
    # fuzzy, pct EXACTLY 1% -> not material -> rules
    ("fuzzy", "amount_mismatch", Decimal("1.00"), Decimal("100.00"), BUCKET_RULES),
    # fuzzy, pct 1.01% -> material by pct -> needs_review
    ("fuzzy", "amount_mismatch", Decimal("1.01"), Decimal("100.00"), BUCKET_NEEDS_REVIEW),
    # stripe_amount NULL with immaterial-by-abs variance -> pct branch skipped -> auto_classifications
    ("deterministic", "amount_mismatch", Decimal("5.00"), None, BUCKET_AUTO_CLASSIFICATIONS),
    # stripe_amount NULL, fuzzy, immaterial-by-abs -> pct branch skipped -> rules
    ("fuzzy", "amount_mismatch", Decimal("5.00"), None, BUCKET_RULES),
    # stripe_amount 0 with immaterial-by-abs variance -> pct branch skipped (ABS>0 guard) -> auto_classifications
    ("deterministic", "amount_mismatch", Decimal("5.00"), Decimal("0"), BUCKET_AUTO_CLASSIFICATIONS),
    # negative variance over $50 by magnitude -> material by abs -> needs_review
    ("deterministic", "amount_mismatch", Decimal("-60.00"), Decimal("100000.00"), BUCKET_NEEDS_REVIEW),
    # negative variance immaterial by magnitude -> auto_classifications
    ("deterministic", "amount_mismatch", Decimal("-5.00"), Decimal("100000.00"), BUCKET_AUTO_CLASSIFICATIONS),
    # negative variance material by pct (magnitude / base) -> needs_review
    ("deterministic", "amount_mismatch", Decimal("-2.00"), Decimal("100.00"), BUCKET_NEEDS_REVIEW),
    # deterministic + no variance -> matches
    ("deterministic", None, Decimal("0"), Decimal("100.00"), BUCKET_MATCHES),
    # fuzzy + no variance -> rules
    ("fuzzy", None, Decimal("0"), Decimal("100.00"), BUCKET_RULES),
    # unmatched (always) -> needs_review
    ("unmatched", "missing_in_netsuite", Decimal("1203.68"), None, BUCKET_NEEDS_REVIEW),
    # exception (always) -> needs_review
    ("exception", "duplicate", Decimal("0"), Decimal("10.00"), BUCKET_NEEDS_REVIEW),
]


async def test_backfill_case_boundary_matches_classify(db, tenant_a):
    """The literal migration-078 backfill CASE agrees with classify() on the edges.

    Guards the hand-written CASE SQL from drifting off the classifier on the
    strict-inequality boundaries ($50 exact vs $50.01; 1% exact vs 1.01%), the
    NULL / 0 stripe_amount relative-branch guards, negative variance, and the
    unmatched/exception/deterministic/fuzzy paths. We seed rows at the column
    server_default (bucket left unset), then run the literal UPDATE...CASE from
    078 and assert each stored bucket equals classify() under $50 / 0.0100.
    """
    run = await create_test_recon_run(db, tenant_a.id)
    seeded: list[tuple[uuid.UUID, str, str | None, Decimal, Decimal | None, str]] = []
    for mt, vt, va, sa_amt, expected in _BACKFILL_BOUNDARY_MATRIX:
        row_id = uuid.uuid4()
        # Insert WITHOUT bucket so the migration's column server_default applies,
        # exactly as a pre-078 row would land before the backfill runs.
        await db.execute(
            text(
                "INSERT INTO reconciliation_results "
                "(id, tenant_id, run_id, match_type, confidence, status, "
                " variance_type, variance_amount, stripe_amount, currency) "
                "VALUES (:id, :tid, :rid, :mt, 1, 'pending', :vt, :va, :sa, 'USD')"
            ),
            {"id": row_id, "tid": tenant_a.id, "rid": run.id, "mt": mt, "vt": vt, "va": va, "sa": sa_amt},
        )
        seeded.append((row_id, mt, vt, va, sa_amt, expected))
    await db.flush()

    # Run the EXACT backfill SQL the migration uses.
    await db.execute(_MIGRATION_078_BACKFILL_SQL, {"rid": run.id})
    await db.flush()

    for row_id, mt, vt, va, sa_amt, expected in seeded:
        stored_bucket = (
            await db.execute(select(ReconciliationResult.bucket).where(ReconciliationResult.id == row_id))
        ).scalar_one()
        # 1. The literal migration CASE landed on the hand-derived expected bucket.
        assert stored_bucket == expected, (
            f"backfill bucket {stored_bucket!r} != expected {expected!r} for ({mt}, {vt}, {va}, {sa_amt})"
        )
        # 2. classify() (single source of truth) agrees with the SQL under the same thresholds.
        py_bucket = classify(
            mt,
            vt,
            va,
            materiality_abs=_MAT_ABS,
            materiality_pct=_MAT_PCT,
            matched_amount=sa_amt,
        )
        assert stored_bucket == py_bucket, (
            f"backfill {stored_bucket!r} != classify() {py_bucket!r} for ({mt}, {vt}, {va}, {sa_amt})"
        )


async def test_run_rollup_counts_persist(db, tenant_a):
    """The 4 rollup counts on a run round-trip through the DB."""
    from datetime import date

    run = ReconciliationRun(
        tenant_id=tenant_a.id,
        date_from=date(2026, 4, 20),
        date_to=date(2026, 4, 24),
        status="completed",
        matches_count=5,
        rules_count=2,
        auto_classifications_count=3,
        needs_review_count=7,
        total_variance=Decimal("0"),
    )
    db.add(run)
    await db.flush()
    await db.refresh(run)
    assert run.matches_count == 5
    assert run.rules_count == 2
    assert run.auto_classifications_count == 3
    assert run.needs_review_count == 7


# ---------------------------------------------------------------------------
# 3. Run-rollup-count backfill — the literal migration-078 rollup UPDATE must
#    recompute the 4 per-run counts from the (already-backfilled) bucket column,
#    so pre-078 runs aren't left at 0/0/0/0.
# ---------------------------------------------------------------------------

# The literal rollup backfill copied verbatim from migration 078 upgrade().
_MIGRATION_078_ROLLUP_SQL = text(
    """
    UPDATE reconciliation_runs r SET
      matches_count = (
        SELECT count(*) FROM reconciliation_results x
        WHERE x.run_id = r.id AND x.bucket = 'matches'),
      rules_count = (
        SELECT count(*) FROM reconciliation_results x
        WHERE x.run_id = r.id AND x.bucket = 'rules'),
      auto_classifications_count = (
        SELECT count(*) FROM reconciliation_results x
        WHERE x.run_id = r.id AND x.bucket = 'auto_classifications'),
      needs_review_count = (
        SELECT count(*) FROM reconciliation_results x
        WHERE x.run_id = r.id AND x.bucket = 'needs_review')
    """
)


async def test_rollup_backfill_recomputes_run_counts(db, tenant_a):
    """The literal migration-078 rollup UPDATE sets each run.*_count from rows.

    Seeds a run whose 4 rollup counts are stale at the column default (0/0/0/0)
    plus a mix of results carrying varied stored buckets, runs the EXACT rollup
    SQL the migration uses, and asserts each count equals the per-bucket row
    count — i.e. a pre-078 run is no longer left at zero. A second run is seeded
    to prove the correlated subquery is scoped per run (no cross-run bleed)."""
    run = await create_test_recon_run(db, tenant_a.id)
    other = await create_test_recon_run(db, tenant_a.id)

    # Bucket distribution for `run` (override bucket= so counts are explicit and
    # independent of classify()): 3 matches, 1 rules, 2 auto_classifications,
    # 4 needs_review.
    distribution = (
        [BUCKET_MATCHES] * 3 + [BUCKET_RULES] * 1 + [BUCKET_AUTO_CLASSIFICATIONS] * 2 + [BUCKET_NEEDS_REVIEW] * 4
    )
    for bucket in distribution:
        await create_test_recon_result(db, tenant_a.id, run.id, bucket=bucket)
    # A different bucket mix on `other` to confirm per-run scoping.
    for bucket in [BUCKET_MATCHES, BUCKET_NEEDS_REVIEW, BUCKET_NEEDS_REVIEW]:
        await create_test_recon_result(db, tenant_a.id, other.id, bucket=bucket)
    await db.flush()

    # Stale at the column default before the backfill runs.
    await db.refresh(run)
    assert (
        run.matches_count,
        run.rules_count,
        run.auto_classifications_count,
        run.needs_review_count,
    ) == (0, 0, 0, 0)

    # Run the EXACT rollup backfill SQL the migration uses.
    await db.execute(_MIGRATION_078_ROLLUP_SQL)
    await db.flush()
    await db.refresh(run)
    await db.refresh(other)

    # Each count equals the per-bucket row count for `run`.
    assert run.matches_count == 3
    assert run.rules_count == 1
    assert run.auto_classifications_count == 2
    assert run.needs_review_count == 4

    # Per-run scoping: `other`'s counts come only from its own rows.
    assert other.matches_count == 1
    assert other.rules_count == 0
    assert other.auto_classifications_count == 0
    assert other.needs_review_count == 2

    # Cross-check each count against a direct per-bucket query (single source of truth).
    for bucket, attr in (
        (BUCKET_MATCHES, "matches_count"),
        (BUCKET_RULES, "rules_count"),
        (BUCKET_AUTO_CLASSIFICATIONS, "auto_classifications_count"),
        (BUCKET_NEEDS_REVIEW, "needs_review_count"),
    ):
        expected = (
            await db.execute(
                text("SELECT count(*) FROM reconciliation_results WHERE run_id = :rid AND bucket = :b"),
                {"rid": run.id, "b": bucket},
            )
        ).scalar_one()
        assert getattr(run, attr) == expected
