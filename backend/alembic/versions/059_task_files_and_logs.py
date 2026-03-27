"""059_task_files_and_logs — task_files and pricing_conversion_logs tables.

Revision ID: 059_task_files
Revises: 058_pricing_cfg
Create Date: 2026-03-26
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "059_task_files"
down_revision = "058_pricing_cfg"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_files",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_type", sa.String(50), nullable=False),
        sa.Column("file_size", sa.Integer, nullable=False),
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("related_message_id", UUID(as_uuid=True), nullable=True),
        sa.Column("metadata_json", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "pricing_conversion_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("input_file_id", UUID(as_uuid=True), nullable=True),
        sa.Column("output_file_id", UUID(as_uuid=True), nullable=True),
        sa.Column("sku_count", sa.Integer, nullable=False),
        sa.Column("currency_count", sa.Integer, nullable=False),
        sa.Column("config_snapshot", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("pricing_conversion_logs")
    op.drop_table("task_files")
