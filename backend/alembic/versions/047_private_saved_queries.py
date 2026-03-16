"""Add created_by and is_public to saved_suiteql_queries."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "047_private_queries"
down_revision = "046_user_auth_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("saved_suiteql_queries", sa.Column("created_by", UUID(as_uuid=True), nullable=True))
    op.add_column("saved_suiteql_queries", sa.Column("is_public", sa.Boolean(), nullable=False, server_default="false"))
    op.create_index("ix_saved_queries_created_by", "saved_suiteql_queries", ["created_by"])


def downgrade() -> None:
    op.drop_index("ix_saved_queries_created_by")
    op.drop_column("saved_suiteql_queries", "is_public")
    op.drop_column("saved_suiteql_queries", "created_by")
