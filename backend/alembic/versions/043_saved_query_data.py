"""Add result_data JSONB to saved_suiteql_queries for snapshot storage."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

revision = "043_saved_query_data"
down_revision = "042_structured_output"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("saved_suiteql_queries", sa.Column("result_data", JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("saved_suiteql_queries", "result_data")
