"""Add netsuite_metadata table for custom field discovery.

Revision ID: 015_add_netsuite_metadata
Revises: 014_add_multi_agent_config
Create Date: 2026-02-17
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "015_add_netsuite_metadata"
down_revision = "014_add_multi_agent_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "netsuite_metadata",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        # Discovery result blobs
        sa.Column("transaction_body_fields", postgresql.JSON(), nullable=True),
        sa.Column("transaction_column_fields", postgresql.JSON(), nullable=True),
        sa.Column("entity_custom_fields", postgresql.JSON(), nullable=True),
        sa.Column("item_custom_fields", postgresql.JSON(), nullable=True),
        sa.Column("custom_record_types", postgresql.JSON(), nullable=True),
        sa.Column("custom_lists", postgresql.JSON(), nullable=True),
        sa.Column("subsidiaries", postgresql.JSON(), nullable=True),
        sa.Column("departments", postgresql.JSON(), nullable=True),
        sa.Column("classifications", postgresql.JSON(), nullable=True),
        sa.Column("locations", postgresql.JSON(), nullable=True),
        # Discovery tracking
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovered_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("discovery_errors", postgresql.JSON(), nullable=True),
        sa.Column("query_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_fields_discovered", sa.Integer(), nullable=False, server_default="0"),
        # Timestamps
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "version", name="uq_netsuite_metadata_tenant_version"),
    )


def downgrade() -> None:
    op.drop_table("netsuite_metadata")
