"""Persist recon bucket + per-run rollup counts + tenant materiality thresholds.

Revision ID: 078_recon_buckets_materiality
Revises: 077_policy_max_rows_50k
Create Date: 2026-06-02

R2a. Moves the four-bucket classification from recompute-on-read to
compute-at-write so the per-tenant materiality threshold can route material
matched-variance lines to needs_review (excluded from bulk-approve).

- ``reconciliation_results.bucket`` VARCHAR(50) NOT NULL DEFAULT 'needs_review',
  with a composite index ``(run_id, bucket)`` for the bucket-filter read path.
- ``reconciliation_runs``: 4 per-bucket rollup counts (INTEGER NOT NULL DEFAULT 0)
  for the runs-list view.
- ``tenant_configs``: ``recon_materiality_abs`` NUMERIC(15,2) NOT NULL DEFAULT 50
  and ``recon_materiality_pct`` NUMERIC(6,4) NOT NULL DEFAULT 0.0100.

The ``bucket`` backfill mirrors ``four_bucket_classifier.classify()`` with the
default thresholds ($50 OR 1% relative). Per-tenant join is unnecessary at
migration time: every tenant inherits the column default, so the constants here
equal each tenant's config. Chains off 077 (the single live head); do NOT touch
the historical 027 head.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "078_recon_buckets_materiality"
down_revision = "077_policy_max_rows_50k"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- reconciliation_results.bucket + composite index ---------------------
    op.add_column(
        "reconciliation_results",
        sa.Column("bucket", sa.String(50), nullable=False, server_default="needs_review"),
    )
    op.create_index(
        "ix_reconciliation_results_run_bucket",
        "reconciliation_results",
        ["run_id", "bucket"],
    )

    # --- reconciliation_runs per-bucket rollup counts -----------------------
    op.add_column(
        "reconciliation_runs",
        sa.Column("matches_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "reconciliation_runs",
        sa.Column("rules_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "reconciliation_runs",
        sa.Column("auto_classifications_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "reconciliation_runs",
        sa.Column("needs_review_count", sa.Integer(), nullable=False, server_default="0"),
    )

    # --- tenant_configs materiality thresholds ------------------------------
    op.add_column(
        "tenant_configs",
        sa.Column("recon_materiality_abs", sa.Numeric(15, 2), nullable=False, server_default="50"),
    )
    op.add_column(
        "tenant_configs",
        sa.Column("recon_materiality_pct", sa.Numeric(6, 4), nullable=False, server_default="0.0100"),
    )

    # --- backfill existing reconciliation_results.bucket --------------------
    # Mirrors four_bucket_classifier.classify() with default materiality
    # ($50 OR 1% relative). All tenants are at the default at migration time.
    op.execute(
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
        """
    )


def downgrade() -> None:
    op.drop_column("tenant_configs", "recon_materiality_pct")
    op.drop_column("tenant_configs", "recon_materiality_abs")

    op.drop_column("reconciliation_runs", "needs_review_count")
    op.drop_column("reconciliation_runs", "auto_classifications_count")
    op.drop_column("reconciliation_runs", "rules_count")
    op.drop_column("reconciliation_runs", "matches_count")

    op.drop_index("ix_reconciliation_results_run_bucket", table_name="reconciliation_results")
    op.drop_column("reconciliation_results", "bucket")
