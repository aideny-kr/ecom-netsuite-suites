"""Add missing metadata columns (scripts, deployments, workflows, custom_list_values).

These columns were added to the SQLAlchemy model but never had migrations.

Revision ID: 027_add_missing_metadata_columns
Revises: 026_add_saved_searches
Create Date: 2026-02-23
"""

from alembic import op
import sqlalchemy as sa

revision = "027_add_missing_metadata_columns"
down_revision = "026_add_saved_searches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("netsuite_metadata", sa.Column("scripts", sa.JSON(), nullable=True))
    op.add_column("netsuite_metadata", sa.Column("script_deployments", sa.JSON(), nullable=True))
    op.add_column("netsuite_metadata", sa.Column("workflows", sa.JSON(), nullable=True))
    op.add_column("netsuite_metadata", sa.Column("custom_list_values", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("netsuite_metadata", "custom_list_values")
    op.drop_column("netsuite_metadata", "workflows")
    op.drop_column("netsuite_metadata", "script_deployments")
    op.drop_column("netsuite_metadata", "scripts")
