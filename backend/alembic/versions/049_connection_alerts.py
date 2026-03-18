"""049_connection_alerts

Create connection_alerts table for admin notifications
when OAuth token refresh fails.

Revision ID: 049_connection_alerts
Revises: 048_onboarding_profile
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "049_connection_alerts"
down_revision = "048_onboarding_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connection_alerts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("connection_type", sa.String(20), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), nullable=False),
        sa.Column("alert_type", sa.String(50), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("dismissed_by", UUID(as_uuid=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("connection_alerts")
