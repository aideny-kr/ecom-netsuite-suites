"""Rename plans: trial→free, enterprise→max

Revision ID: 005_rename_plans
Revises: 004_ai_provider_byok
Create Date: 2026-02-16
"""

from alembic import op

revision = "005_rename_plans"
down_revision = "004_ai_provider_byok"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE tenants SET plan = 'free' WHERE plan = 'trial'")
    op.execute("UPDATE tenants SET plan = 'max' WHERE plan = 'enterprise'")
    op.execute("ALTER TABLE tenants ALTER COLUMN plan SET DEFAULT 'free'")


def downgrade() -> None:
    op.execute("UPDATE tenants SET plan = 'trial' WHERE plan = 'free'")
    op.execute("UPDATE tenants SET plan = 'enterprise' WHERE plan = 'max'")
    op.execute("ALTER TABLE tenants ALTER COLUMN plan SET DEFAULT 'trial'")
