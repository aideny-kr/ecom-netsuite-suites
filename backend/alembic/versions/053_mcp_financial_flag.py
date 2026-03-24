"""Add use_mcp_financial_reports flag to tenant_configs.

Previously applied directly to production DB without a migration file.
This migration ensures CI and fresh environments have the column.
"""

from alembic import op
import sqlalchemy as sa

revision = "053_mcp_financial_flag"
down_revision = "052_rag_partition_id"


def upgrade() -> None:
    op.add_column(
        "tenant_configs",
        sa.Column(
            "use_mcp_financial_reports",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant_configs", "use_mcp_financial_reports")
