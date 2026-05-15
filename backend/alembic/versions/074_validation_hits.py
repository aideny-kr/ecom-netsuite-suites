"""validation_hits + workspace_runs validate columns

Revision ID: 074_validation_hits
Revises: 073_chat_disclosure_events
Create Date: 2026-05-09
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers
revision = "074_validation_hits"
down_revision = "073_chat_disclosure_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "validation_hits",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspace_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("code", sa.String(128), nullable=True),
        sa.Column("rule_id", sa.String(256), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_validation_hits_tenant_id", "validation_hits", ["tenant_id"])
    op.create_index("ix_validation_hits_run_id", "validation_hits", ["run_id"])
    op.create_index("ix_validation_hits_fingerprint", "validation_hits", ["fingerprint"])

    op.add_column("workspace_runs", sa.Column("validator_engine", sa.String(32), nullable=True))
    op.add_column("workspace_runs", sa.Column("parser_version", sa.String(16), nullable=True))
    op.add_column(
        "workspace_runs",
        sa.Column("has_errors", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "workspace_runs",
        sa.Column("has_warnings", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("workspace_runs", sa.Column("gate_status", sa.String(32), nullable=True))
    op.add_column("workspace_runs", sa.Column("snapshot_hash", sa.String(64), nullable=True))
    op.create_index("ix_workspace_runs_snapshot_hash", "workspace_runs", ["snapshot_hash"])


def downgrade() -> None:
    op.drop_index("ix_workspace_runs_snapshot_hash", table_name="workspace_runs")
    op.drop_column("workspace_runs", "snapshot_hash")
    op.drop_column("workspace_runs", "gate_status")
    op.drop_column("workspace_runs", "has_warnings")
    op.drop_column("workspace_runs", "has_errors")
    op.drop_column("workspace_runs", "parser_version")
    op.drop_column("workspace_runs", "validator_engine")
    op.drop_index("ix_validation_hits_fingerprint", table_name="validation_hits")
    op.drop_index("ix_validation_hits_run_id", table_name="validation_hits")
    op.drop_index("ix_validation_hits_tenant_id", table_name="validation_hits")
    op.drop_table("validation_hits")
