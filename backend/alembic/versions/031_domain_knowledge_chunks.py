"""Add domain_knowledge_chunks table for JIT domain knowledge retrieval

Revision ID: 031_domain_knowledge_chunks
Revises: 030_metered_billing
Create Date: 2026-02-25 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "031_domain_knowledge_chunks"
down_revision: Union[str, None] = "030_metered_billing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "domain_knowledge_chunks",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("source_uri", sa.String(255), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("topic_tags", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("source_type", sa.String(50), nullable=False, server_default="expert_rules"),
        sa.Column("is_deprecated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_uri", "chunk_index", name="uq_dk_source_chunk"),
    )

    # GIN index on topic_tags for tag-based filtering
    op.create_index("ix_dk_topic_tags", "domain_knowledge_chunks", ["topic_tags"], postgresql_using="gin")

    # HNSW index on embedding for fast vector similarity search
    op.execute(
        "CREATE INDEX ix_dk_embedding_hnsw ON domain_knowledge_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)"
    )


def downgrade() -> None:
    op.drop_index("ix_dk_embedding_hnsw", table_name="domain_knowledge_chunks")
    op.drop_index("ix_dk_topic_tags", table_name="domain_knowledge_chunks")
    op.drop_table("domain_knowledge_chunks")
