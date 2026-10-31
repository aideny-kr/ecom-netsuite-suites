"""Bind the versioned trusted project projection; legacy confirmations stay closed."""

import sqlalchemy as sa

from alembic import op

revision = "104_workspace_project_format"
down_revision = "103_workspace_target_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspace_deploy_tokens", sa.Column("project_format", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("workspace_deploy_tokens", "project_format")
