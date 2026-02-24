"""Add saved_searches column to netsuite_metadata.

Revision ID: 026_add_saved_searches
Revises: 025_tenant_entity_mapping
Create Date: 2026-02-23
"""

import sqlalchemy as sa

from alembic import op

revision = "026_add_saved_searches"
down_revision = "025_tenant_entity_mapping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("netsuite_metadata", sa.Column("saved_searches", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("netsuite_metadata", "saved_searches")
