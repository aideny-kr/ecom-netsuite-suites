"""Add reconciliation_runs and reconciliation_results tables."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON, UUID

from alembic import op

revision = "062_recon_results"
down_revision = "061_session_agent"


def upgrade() -> None:
    # --- reconciliation_runs ---
    op.create_table(
        "reconciliation_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("date_from", sa.Date, nullable=False),
        sa.Column("date_to", sa.Date, nullable=False),
        sa.Column("subsidiary_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("total_payouts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_deposits", sa.Integer, nullable=False, server_default="0"),
        sa.Column("matched_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("exception_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("unmatched_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_variance", sa.Numeric(15, 2), nullable=False, server_default="0"),
        sa.Column("parameters", JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # --- reconciliation_results ---
    op.create_table(
        "reconciliation_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("reconciliation_runs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("payout_id", UUID(as_uuid=True), sa.ForeignKey("payouts.id"), nullable=True),
        sa.Column("deposit_id", UUID(as_uuid=True), sa.ForeignKey("netsuite_postings.id"), nullable=True),
        sa.Column("match_type", sa.String(50), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("stripe_amount", sa.Numeric(15, 2), nullable=True),
        sa.Column("netsuite_amount", sa.Numeric(15, 2), nullable=True),
        sa.Column("variance_amount", sa.Numeric(15, 2), nullable=False, server_default="0"),
        sa.Column("variance_type", sa.String(50), nullable=True),
        sa.Column("variance_explanation", sa.Text, nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("match_rule", sa.String(255), nullable=True),
        sa.Column("evidence", JSON, nullable=True),
        sa.Column("approved_by", UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_index(
        "ix_recon_results_run_status",
        "reconciliation_results",
        ["run_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_recon_results_run_status", table_name="reconciliation_results")
    op.drop_table("reconciliation_results")
    op.drop_table("reconciliation_runs")
