"""recon_resolution_proposals — Phase 1 of the summary-first recon rework.

One row per exception result, written by the ResolutionPlanner (Phase 1) or
ResolutionAgent (Phase 2). Groups are computed, never stored. Partial unique
index = ONE active proposal per result (superseded/rejected rows are history).
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "089_recon_resolution_proposals"
down_revision = "088_report_auto_refresh"
branch_labels = None
depends_on = None

ACTIVE_STATUSES = "('proposed','approved','posting','posted','post_failed')"


def upgrade() -> None:
    op.create_table(
        "recon_resolution_proposals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("reconciliation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "result_id",
            UUID(as_uuid=True),
            sa.ForeignKey("reconciliation_results.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("root_cause", sa.String(50), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("booking_vehicle", sa.String(50), nullable=False),
        sa.Column("group_key", sa.String(160), nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="planner"),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column("evidence", JSONB(), nullable=True),
        sa.Column("proposed_amount", sa.Numeric(15, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("above_materiality", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column("failure_reason", sa.String(50), nullable=True),
        sa.Column("netsuite_record_refs", JSONB(), nullable=True),
        sa.Column("correlation_id", sa.String(255), nullable=True),
        # Denormalized cross-run double-posting guard key (from result.evidence).
        sa.Column("charge_source_id", sa.String(255), nullable=True),
        sa.Column("decided_by", UUID(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_recon_resolution_proposals_tenant", "recon_resolution_proposals", ["tenant_id"])
    op.create_index(
        "ix_recon_resolution_proposals_run_group",
        "recon_resolution_proposals",
        ["run_id", "root_cause", "action", "booking_vehicle"],
    )
    op.create_index("ix_recon_resolution_proposals_corr", "recon_resolution_proposals", ["correlation_id"])
    op.create_index(
        "ix_recon_resolution_proposals_charge",
        "recon_resolution_proposals",
        ["tenant_id", "charge_source_id"],
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_recon_resolution_proposals_active_result "
        "ON recon_resolution_proposals (result_id) "
        f"WHERE status IN {ACTIVE_STATUSES}"
    )
    op.execute("ALTER TABLE recon_resolution_proposals ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY recon_resolution_proposals_tenant_isolation ON recon_resolution_proposals
        USING (tenant_id = get_current_tenant_id())
        WITH CHECK (tenant_id = get_current_tenant_id())
    """)
    # load-bearing on Supabase (owner != BYPASSRLS)
    op.execute("ALTER TABLE recon_resolution_proposals FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("recon_resolution_proposals")
