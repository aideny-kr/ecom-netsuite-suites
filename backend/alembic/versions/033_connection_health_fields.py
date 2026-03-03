"""Add health-check columns to connections and mcp_connectors.

Revision ID: 033_connection_health_fields
Revises: 032_saved_suiteql_queries
"""

from alembic import op
import sqlalchemy as sa

revision = "033_connection_health_fields"
down_revision = "032_saved_suiteql_queries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "connections",
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "connections",
        sa.Column("error_reason", sa.String(500), nullable=True),
    )
    op.add_column(
        "mcp_connectors",
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "mcp_connectors",
        sa.Column("error_reason", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mcp_connectors", "error_reason")
    op.drop_column("mcp_connectors", "last_health_check_at")
    op.drop_column("connections", "error_reason")
    op.drop_column("connections", "last_health_check_at")
