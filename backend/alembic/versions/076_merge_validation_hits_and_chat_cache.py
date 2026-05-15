"""Merge validation_hits and chat cache token migration heads.

Revision ID: 076_merge_validation_cache
Revises: 074_validation_hits, 075_chat_cache_tokens
Create Date: 2026-05-15
"""

# revision identifiers
revision = "076_merge_validation_cache"
down_revision = ("074_validation_hits", "075_chat_cache_tokens")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Merge revision only. The parent migrations carry the schema changes.
    pass


def downgrade() -> None:
    # Keep both parent branches intact when downgrading from the merge node.
    pass
