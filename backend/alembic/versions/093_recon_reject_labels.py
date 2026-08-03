"""recon reject labels — negative-outcome capture on reconciliation_results

Adds the columns that let a reviewer record WHY a match was wrong, and whether
that row was inside the autonomy envelope at the time.

Every column is nullable with no backfill, on purpose. Rows decided before this
migration have no reject reason and no eligibility snapshot, and inventing one
would fabricate evidence for the exact metric this exists to make honest.
NULL here means "we did not record it", which is the truth.

Revision ID: 093_recon_reject_labels
Revises: 092_user_dashboard_preference
Create Date: 2026-08-03

Numbered 093, not 092: origin/main landed 092_user_dashboard_preference (PR #189)
parented to 091 while this branch was open. Two migrations sharing a parent gives
alembic parallel heads, and the fix is to RE-PARENT (linearize) — never a merge
migration, which breaks `downgrade -1`.
"""

import sqlalchemy as sa

from alembic import op

revision = "093_recon_reject_labels"
down_revision = "092_user_dashboard_preference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reconciliation_results", sa.Column("rejected_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("reconciliation_results", sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("reconciliation_results", sa.Column("reject_reason", sa.String(length=50), nullable=True))
    op.add_column("reconciliation_results", sa.Column("reject_note", sa.Text(), nullable=True))
    op.add_column("reconciliation_results", sa.Column("envelope_eligible_at_decision", sa.Boolean(), nullable=True))
    op.add_column("reconciliation_results", sa.Column("counts_as_false_positive", sa.Boolean(), nullable=True))

    # The calibration query is "envelope-eligible rows a human decided", scoped
    # per tenant. Partial index: only decided rows matter, and the rejected
    # population stays small relative to the table.
    op.create_index(
        "ix_recon_results_reject_labels",
        "reconciliation_results",
        ["tenant_id", "envelope_eligible_at_decision", "counts_as_false_positive"],
        postgresql_where=sa.text("reject_reason IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_recon_results_reject_labels", table_name="reconciliation_results")
    op.drop_column("reconciliation_results", "counts_as_false_positive")
    op.drop_column("reconciliation_results", "envelope_eligible_at_decision")
    op.drop_column("reconciliation_results", "reject_note")
    op.drop_column("reconciliation_results", "reject_reason")
    op.drop_column("reconciliation_results", "rejected_at")
    op.drop_column("reconciliation_results", "rejected_by")
