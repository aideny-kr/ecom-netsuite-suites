"""Add use_mcp_financial_reports flag to tenant_configs.

Previously applied directly to production DB without a migration file.
This migration ensures CI and fresh environments have the column.
Uses execute() with IF NOT EXISTS for idempotency on existing DBs.
"""

from alembic import op

revision = "053_mcp_financial_flag"
down_revision = "052_rag_partition_id"


def upgrade() -> None:
    # Use raw SQL with IF NOT EXISTS — column may already exist on prod/staging
    op.execute(
        "ALTER TABLE tenant_configs ADD COLUMN IF NOT EXISTS use_mcp_financial_reports BOOLEAN NOT NULL DEFAULT true"
    )


def downgrade() -> None:
    op.drop_column("tenant_configs", "use_mcp_financial_reports")
