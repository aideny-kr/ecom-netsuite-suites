"""Add cache_creation_tokens / cache_read_tokens columns to chat_messages.

Promotes Anthropic prompt-cache stats from the audit_events JSONB payload
to first-class columns on chat_messages so cache hit rate is queryable
without parsing JSON. Source: codex review of the May 2026 prompt-cache
audit.

Nullable — historic rows lack the data. New writes populate from
``response.usage.cache_creation_input_tokens`` /
``response.usage.cache_read_input_tokens``.

Numbered 075 to leave room for the in-flight ``074_validation_hits`` migration
on feat/workspace-validate-ux. ``down_revision`` chains to 073 (the current
main head); when both PRs land, alembic branched-history is resolved with a
merge revision (standard practice).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "075_chat_cache_tokens"
down_revision = "073_chat_disclosure_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("cache_creation_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "cache_read_tokens")
    op.drop_column("chat_messages", "cache_creation_tokens")
