"""drive_chunks HNSW vector index for cosine similarity

Revision ID: 072_drive_vec_idx
Revises: 071_folders_setnull
Create Date: 2026-04-26
"""

from alembic import op

revision = "072_drive_vec_idx"
down_revision = "071_folders_setnull"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # HNSW with vector_cosine_ops matches the retriever's cosine_distance query
    # (`embedding <=> $1`). Defaults (m=16, ef_construction=64) — appropriate at
    # our scale (~thousands of chunks). Build is O(N log N), one-time cost.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_drive_chunks_embedding_hnsw "
        "ON drive_chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_drive_chunks_embedding_hnsw")
