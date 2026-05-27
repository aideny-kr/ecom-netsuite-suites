"""Raise policy_profiles.max_rows_per_query default 1000 -> 50000.

Revision ID: 077_policy_max_rows_50k
Revises: 076_ws_deploy_tokens
Create Date: 2026-05-27

The global SuiteQL row cap is bumped to 50000 in `Settings`. Mirror that on
the per-tenant policy column so new policies inherit the higher default at
the database level, and bring along existing rows that were still pinned to
the original 1000 default (tenant customizations are preserved).
"""

from alembic import op

revision = "077_policy_max_rows_50k"
down_revision = "076_ws_deploy_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE policy_profiles ALTER COLUMN max_rows_per_query SET DEFAULT 50000")
    op.execute("UPDATE policy_profiles SET max_rows_per_query = 50000 WHERE max_rows_per_query = 1000")


def downgrade() -> None:
    op.execute("ALTER TABLE policy_profiles ALTER COLUMN max_rows_per_query SET DEFAULT 1000")
    op.execute("UPDATE policy_profiles SET max_rows_per_query = 1000 WHERE max_rows_per_query = 50000")
