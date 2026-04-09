"""Fix chat_disclosure_events FK ondelete rules (CASCADE / SET NULL)."""

from alembic import op

revision = "066_fix_disclosure_event_fks"
down_revision = "065_disclosure_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Re-create session_id FK with ON DELETE CASCADE
    op.drop_constraint(
        "chat_disclosure_events_session_id_fkey",
        "chat_disclosure_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "chat_disclosure_events_session_id_fkey",
        "chat_disclosure_events",
        "chat_sessions",
        ["session_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Re-create message_id FK with ON DELETE SET NULL
    op.drop_constraint(
        "chat_disclosure_events_message_id_fkey",
        "chat_disclosure_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "chat_disclosure_events_message_id_fkey",
        "chat_disclosure_events",
        "chat_messages",
        ["message_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Revert message_id FK to no ondelete rule
    op.drop_constraint(
        "chat_disclosure_events_message_id_fkey",
        "chat_disclosure_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "chat_disclosure_events_message_id_fkey",
        "chat_disclosure_events",
        "chat_messages",
        ["message_id"],
        ["id"],
    )

    # Revert session_id FK to no ondelete rule
    op.drop_constraint(
        "chat_disclosure_events_session_id_fkey",
        "chat_disclosure_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "chat_disclosure_events_session_id_fkey",
        "chat_disclosure_events",
        "chat_sessions",
        ["session_id"],
        ["id"],
    )
