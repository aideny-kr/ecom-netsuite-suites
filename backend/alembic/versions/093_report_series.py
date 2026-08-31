"""093 — report_series table + reports.series_id/period (rolling-period Stage 1, Task 2).

Reports stay immutable artifacts of one period (docs/superpowers/plans/2026-08-06-
rolling-period-stage1.md § Shape — rolling a single report in place was rejected: its
title/narrative are written once at compose and never recomputed by refresh, and
refresh publishes a version unconditionally every tick). A `report_series` row instead
tracks "the newest report for playbook X, tenant Y" — the dashboard wall follows the
series, not a mutated report.

report_series: one row per (tenant, playbook_key). UNIQUE (tenant_id, playbook_key)
enforces "one tracking series per playbook per tenant". RLS mirrors 084/092's shape:
ENABLE + FORCE, USING/WITH CHECK both pinned to get_current_tenant_id(), no OR-SYSTEM
branch (report_series rows are never SYSTEM-owned).

reports gains:
  - series_id, FK -> report_series.id ON DELETE SET NULL: deleting a series must NOT
    delete its reports — they remain valid standalone artifacts, just no longer
    tracked. This is the FK's job; no application code or ORM relationship() drives it.
  - period, the canonical "Mon YYYY" the report covers, denormalized from recipe_json
    so finding the newest report in a series doesn't require parsing JSON.
  - a PARTIAL unique index on (series_id, period) WHERE both are non-null — this is
    what makes Stage 2's scheduled compose idempotent (composing the same period twice
    for the same series collides instead of duplicating). Two NULLs remain allowed:
    every existing report has both NULL and stays that way — no backfill.

Verify single head before/after (alembic heads); never a merge migration.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "093_report_series"
down_revision = "096_celigo_flow_step_provenance"  # re-parented 2026-08-30: main landed
# 094/095/096_celigo_* off the SAME parent (093_recon_reject_labels) while this branch was
# in review, which would leave TWO heads. Linearized by re-parenting onto their tip rather
# than adding a merge migration -- a merge migration breaks `downgrade -1` (see memory
# feedback_merge_migration_breaks_downgrade). The 093_/094_ filename prefixes now sort
# BEFORE the celigo files they depend on; alembic orders by revision graph, not filename,
# so this is cosmetic only -- the ids are load-bearing and are left untouched.
# landed on main first (PR #193) and also claimed 092 as its parent. Two children of
# one revision is the parallel-head case that breaks `alembic upgrade head` on every
# deploy. Re-parented (LINEARIZED) rather than resolved with a merge migration,
# because a merge migration breaks `downgrade -1`. The file keeps its 093_ name to
# avoid churning the down_revision of 094 that points at it; the number is a label,
# the chain is what matters.
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "report_series",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("playbook_key", sa.Text(), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "playbook_key", name="uq_report_series_tenant_playbook"),
    )
    op.create_index("ix_report_series_tenant_id", "report_series", ["tenant_id"])

    # RLS — NO OR-SYSTEM branch (report_series rows are never SYSTEM-owned), mirrors 084/092.
    op.execute("ALTER TABLE report_series ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY report_series_tenant_isolation ON report_series
        USING (tenant_id = get_current_tenant_id())
        WITH CHECK (tenant_id = get_current_tenant_id())
    """)
    op.execute("ALTER TABLE report_series FORCE ROW LEVEL SECURITY")  # load-bearing on Supabase (owner != BYPASSRLS)

    op.add_column(
        "reports",
        sa.Column(
            "series_id",
            UUID(as_uuid=True),
            sa.ForeignKey("report_series.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("reports", sa.Column("period", sa.Text(), nullable=True))
    op.create_index("ix_reports_series_id", "reports", ["series_id"])
    op.create_index(
        "uq_reports_series_id_period",
        "reports",
        ["series_id", "period"],
        unique=True,
        postgresql_where=sa.text("series_id IS NOT NULL AND period IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_reports_series_id_period", table_name="reports")
    op.drop_index("ix_reports_series_id", table_name="reports")
    op.drop_column("reports", "period")
    op.drop_column("reports", "series_id")

    op.execute("DROP POLICY IF EXISTS report_series_tenant_isolation ON report_series")
    op.drop_index("ix_report_series_tenant_id", table_name="report_series")
    op.drop_table("report_series")
