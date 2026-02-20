"""Add pessimistic locking columns to workspace_files

Adds locked_by (FK -> users) and locked_at columns to prevent concurrent
patch conflicts on the same file.

Revision ID: 022_workspace_file_locking
Revises: 021_rls_stable_function
Create Date: 2026-02-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "022_workspace_file_locking"
down_revision = "021_rls_stable_function"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_files",
        sa.Column("locked_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
    )
    op.add_column(
        "workspace_files",
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_workspace_files_locked_by", "workspace_files", ["locked_by"])


def downgrade() -> None:
    op.drop_index("ix_workspace_files_locked_by", table_name="workspace_files")
    op.drop_column("workspace_files", "locked_at")
    op.drop_column("workspace_files", "locked_by")
