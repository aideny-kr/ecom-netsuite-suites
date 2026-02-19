"""Add auth_type column to connections table."""

import sqlalchemy as sa

from alembic import op

revision = "020_add_auth_type"
down_revision = "019_add_netsuite_file_id"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "connections",
        sa.Column("auth_type", sa.String(50), nullable=True),
    )


def downgrade():
    op.drop_column("connections", "auth_type")
