"""050_workspace_script_type

Add script_type column to workspace_files for auto-organization by
SuiteScript type (UserEventScript, ClientScript, etc.).

Revision ID: 050_ws_script_type
Revises: 049_connection_alerts
"""

from alembic import op
import sqlalchemy as sa

revision = "050_ws_script_type"
down_revision = "049_connection_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_files",
        sa.Column("script_type", sa.String(50), nullable=True),
    )
    op.create_index(
        "ix_workspace_files_ws_script_type",
        "workspace_files",
        ["workspace_id", "script_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_files_ws_script_type", table_name="workspace_files")
    op.drop_column("workspace_files", "script_type")
