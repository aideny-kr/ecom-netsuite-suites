"""Add netsuite_file_id column to workspace_files for NetSuite origin tracking."""

import sqlalchemy as sa

from alembic import op

revision = "019_add_netsuite_file_id"
down_revision = "018_netsuite_api_log"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workspace_files",
        sa.Column("netsuite_file_id", sa.String(50), nullable=True),
    )
    op.create_index(
        "ix_workspace_files_netsuite_file_id",
        "workspace_files",
        ["workspace_id", "netsuite_file_id"],
    )


def downgrade():
    op.drop_index("ix_workspace_files_netsuite_file_id")
    op.drop_column("workspace_files", "netsuite_file_id")
