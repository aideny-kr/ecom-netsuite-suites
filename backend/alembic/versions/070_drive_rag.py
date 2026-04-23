"""drive_rag: drive_folders, drive_files, drive_chunks

Revision ID: 070_drive_rag
Revises: 069_agent_lab_runs
Create Date: 2026-04-22
"""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSON, UUID

from alembic import op

# revision identifiers
revision = "070_drive_rag"
down_revision = "069_agent_lab_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "drive_folders",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("folder_id", sa.String(128), nullable=False),
        sa.Column("folder_name", sa.String(512), nullable=False),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("sync_status", sa.String(20), nullable=False, server_default="idle"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text, nullable=True),
        sa.Column(
            "created_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
        sa.UniqueConstraint(
            "tenant_id", "folder_id", name="uq_drive_folder_tenant_folder"
        ),
    )
    op.create_index(
        "ix_drive_folders_tenant_enabled",
        "drive_folders",
        ["tenant_id", "is_enabled"],
    )

    op.create_table(
        "drive_files",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "folder_id",
            UUID(as_uuid=True),
            sa.ForeignKey("drive_folders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("drive_file_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("web_view_link", sa.Text, nullable=False),
        sa.Column("modified_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chunk_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_extract_error", sa.Text, nullable=True),
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
        sa.UniqueConstraint(
            "tenant_id", "drive_file_id", name="uq_drive_file_tenant_file"
        ),
    )
    op.create_index("ix_drive_files_folder", "drive_files", ["folder_id"])

    op.create_table(
        "drive_chunks",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "file_id",
            UUID(as_uuid=True),
            sa.ForeignKey("drive_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("token_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("metadata", JSON, nullable=True),
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
    op.create_index("ix_drive_chunks_tenant", "drive_chunks", ["tenant_id"])
    op.create_index("ix_drive_chunks_file", "drive_chunks", ["file_id"])


def downgrade() -> None:
    op.drop_index("ix_drive_chunks_file", table_name="drive_chunks")
    op.drop_index("ix_drive_chunks_tenant", table_name="drive_chunks")
    op.drop_table("drive_chunks")
    op.drop_index("ix_drive_files_folder", table_name="drive_files")
    op.drop_table("drive_files")
    op.drop_index("ix_drive_folders_tenant_enabled", table_name="drive_folders")
    op.drop_table("drive_folders")
