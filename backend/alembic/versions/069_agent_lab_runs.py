"""agent_lab_runs table + partial unique index

Revision ID: 069_agent_lab_runs
Revises: 068_revoke_recon_ops
Create Date: 2026-04-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers
revision = "069_agent_lab_runs"
down_revision = "068_revoke_recon_ops"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_lab_runs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "triggered_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("case_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("total_cases", sa.Integer, nullable=False),
        sa.Column("cases_completed", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("cost_usd_actual", sa.Float, nullable=False, server_default=sa.text("0.0")),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_index("agent_lab_runs_tenant_id_idx", "agent_lab_runs", ["tenant_id"])
    op.create_index(
        "agent_lab_runs_tenant_kind_started_idx",
        "agent_lab_runs",
        ["tenant_id", "kind", "started_at"],
    )
    op.create_index(
        "agent_lab_runs_single_running",
        "agent_lab_runs",
        ["tenant_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("agent_lab_runs_single_running", table_name="agent_lab_runs")
    op.drop_index("agent_lab_runs_tenant_kind_started_idx", table_name="agent_lab_runs")
    op.drop_index("agent_lab_runs_tenant_id_idx", table_name="agent_lab_runs")
    op.drop_table("agent_lab_runs")
