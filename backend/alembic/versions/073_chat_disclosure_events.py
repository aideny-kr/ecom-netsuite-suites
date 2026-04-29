"""chat_disclosure_events table for HITL telemetry (Plan Mode + write-confirm).

Conceptually cherry-picked from closed PR #29 (was 065_disclosure_events). Renumbered to 073
to land after the current head 072_drive_vec_idx. Schema diverges from the original — uses
chat_session_id / chat_message_id / payload (JSONB) per the Plan Mode design doc.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

# revision identifiers
revision = "073_chat_disclosure_events"
down_revision = "072_drive_vec_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_disclosure_events",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chat_session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chat_message_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column(
            "payload",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_index(
        "ix_chat_disclosure_events_tenant_session",
        "chat_disclosure_events",
        ["tenant_id", "chat_session_id"],
    )
    op.create_index(
        "ix_chat_disclosure_events_event_type",
        "chat_disclosure_events",
        ["event_type"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chat_disclosure_events_event_type",
        table_name="chat_disclosure_events",
    )
    op.drop_index(
        "ix_chat_disclosure_events_tenant_session",
        table_name="chat_disclosure_events",
    )
    op.drop_table("chat_disclosure_events")
