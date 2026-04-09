"""Add chat_disclosure_events telemetry table."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "065_disclosure_events"
down_revision = "064_disclosure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_disclosure_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=True),
        sa.Column("metadata", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_chat_disclosure_events_tenant_id",
        "chat_disclosure_events",
        ["tenant_id"],
    )
    op.create_index(
        "ix_chat_disclosure_events_session_id",
        "chat_disclosure_events",
        ["session_id"],
    )
    op.create_index(
        "ix_chat_disclosure_events_event_type",
        "chat_disclosure_events",
        ["event_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_disclosure_events_event_type", table_name="chat_disclosure_events")
    op.drop_index("ix_chat_disclosure_events_session_id", table_name="chat_disclosure_events")
    op.drop_index("ix_chat_disclosure_events_tenant_id", table_name="chat_disclosure_events")
    op.drop_table("chat_disclosure_events")
