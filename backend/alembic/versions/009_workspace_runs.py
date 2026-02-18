"""Workspace runs and artifacts tables

Revision ID: 009_workspace_runs
Revises: 008_onboarding_chat_api
Create Date: 2026-02-17
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "009_workspace_runs"
down_revision = "008_onboarding_chat_api"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- workspace_runs ---
    op.create_table(
        "workspace_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "changeset_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspace_changesets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("run_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(50), server_default="queued", nullable=False),
        sa.Column("triggered_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("command", sa.Text, nullable=True),
        sa.Column("exit_code", sa.Integer, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_workspace_runs_tenant_id", "workspace_runs", ["tenant_id"])
    op.create_index("ix_workspace_runs_workspace_id", "workspace_runs", ["workspace_id"])
    op.create_index("ix_workspace_runs_changeset_id", "workspace_runs", ["changeset_id"])
    op.create_index("ix_workspace_runs_correlation_id", "workspace_runs", ["correlation_id"])

    op.execute("ALTER TABLE workspace_runs ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY workspace_runs_tenant_isolation ON workspace_runs
        USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
    """)

    # --- workspace_artifacts ---
    op.create_table(
        "workspace_artifacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspace_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("artifact_type", sa.String(50), nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("size_bytes", sa.Integer, server_default="0", nullable=False),
        sa.Column("sha256_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_workspace_artifacts_tenant_id", "workspace_artifacts", ["tenant_id"])
    op.create_index("ix_workspace_artifacts_run_id", "workspace_artifacts", ["run_id"])

    op.execute("ALTER TABLE workspace_artifacts ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY workspace_artifacts_tenant_isolation ON workspace_artifacts
        USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS workspace_artifacts_tenant_isolation ON workspace_artifacts")
    op.execute("ALTER TABLE workspace_artifacts DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS workspace_runs_tenant_isolation ON workspace_runs")
    op.execute("ALTER TABLE workspace_runs DISABLE ROW LEVEL SECURITY")

    op.drop_table("workspace_artifacts")
    op.drop_table("workspace_runs")
