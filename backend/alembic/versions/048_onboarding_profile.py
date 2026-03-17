"""Add onboarding_profile JSON column to tenant_configs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

revision = "048_onboarding_profile"
down_revision = "047_private_queries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_configs", sa.Column("onboarding_profile", JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_configs", "onboarding_profile")
