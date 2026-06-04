"""Tenant-configurable order-reference extraction pattern.

Revision ID: 079_order_ref_pattern
Revises: 078_recon_buckets_materiality
Create Date: 2026-06-03

R3 Part 1. Adds ``tenant_configs.order_ref_pattern`` VARCHAR(200) NULLABLE so a
tenant can override the hardcoded ``R\\d{9}`` order-key extraction. NULL is the
sentinel meaning "use the engine default" (``DEFAULT_ORDER_REF_PATTERN``), so
NO server_default and NO backfill — every existing tenant (e.g. Framework) keeps
extracting byte-identically to the prior hardcoded pattern.

Chains off 078 (the single live head); do NOT touch the historical 027 head.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "079_order_ref_pattern"
down_revision = "078_recon_buckets_materiality"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_configs",
        sa.Column("order_ref_pattern", sa.String(200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_configs", "order_ref_pattern")
