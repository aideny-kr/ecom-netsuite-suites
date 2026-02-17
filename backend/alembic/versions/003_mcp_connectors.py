"""MCP Connectors table for external MCP server connections

Revision ID: 003_mcp_connectors
Revises: 002_chat_module
Create Date: 2026-02-16
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSON
import uuid

revision = "003_mcp_connectors"
down_revision = "002_chat_module"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_connectors",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("server_url", sa.String(1024), nullable=False),
        sa.Column("auth_type", sa.String(20), server_default="none", nullable=False),
        sa.Column("encrypted_credentials", sa.Text, nullable=True),
        sa.Column("encryption_key_version", sa.Integer, server_default="1", nullable=False),
        sa.Column("status", sa.String(50), server_default="active", nullable=False),
        sa.Column("discovered_tools", JSON, nullable=True),
        sa.Column("is_enabled", sa.Boolean, server_default="true", nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("metadata_json", JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_mcp_connectors_tenant_id", "mcp_connectors", ["tenant_id"])

    # RLS policy
    op.execute("ALTER TABLE mcp_connectors ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY mcp_connectors_tenant_isolation ON mcp_connectors
        USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS mcp_connectors_tenant_isolation ON mcp_connectors")
    op.execute("ALTER TABLE mcp_connectors DISABLE ROW LEVEL SECURITY")
    op.drop_table("mcp_connectors")
