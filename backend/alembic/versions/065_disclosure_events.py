"""065_disclosure_events: telemetry table for disclosure metrics

Revision ID: 065_disclosure_events
Revises: 064_disclosure
Create Date: 2026-04-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "065_disclosure_events"
down_revision = "064_disclosure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_disclosure_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id"), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_messages.id"), nullable=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=True),
        sa.Column("event_metadata", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_disclosure_events_tenant_created", "chat_disclosure_events", ["tenant_id", "created_at"])
    op.create_index("ix_disclosure_events_event_type", "chat_disclosure_events", ["event_type"])
    op.create_index("ix_disclosure_events_session", "chat_disclosure_events", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_disclosure_events_session", table_name="chat_disclosure_events")
    op.drop_index("ix_disclosure_events_event_type", table_name="chat_disclosure_events")
    op.drop_index("ix_disclosure_events_tenant_created", table_name="chat_disclosure_events")
    op.drop_table("chat_disclosure_events")
