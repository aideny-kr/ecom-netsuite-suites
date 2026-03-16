"""Add auth_provider and google_sub columns to users."""

from alembic import op
import sqlalchemy as sa

revision = "046_user_auth_provider"
down_revision = "045_invites_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("auth_provider", sa.String(20), nullable=False, server_default="email"))
    op.add_column("users", sa.Column("google_sub", sa.String(255), nullable=True))
    op.create_unique_constraint("uq_users_google_sub", "users", ["google_sub"])


def downgrade() -> None:
    op.drop_constraint("uq_users_google_sub", "users")
    op.drop_column("users", "google_sub")
    op.drop_column("users", "auth_provider")
