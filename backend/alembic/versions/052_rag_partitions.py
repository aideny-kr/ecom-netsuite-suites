"""Add partition_id to domain_knowledge_chunks for per-agent RAG isolation."""

from alembic import op
import sqlalchemy as sa

revision = "052_rag_partition_id"
down_revision = "051_agent_configs"


def upgrade() -> None:
    op.add_column("domain_knowledge_chunks", sa.Column("partition_id", sa.String(64), nullable=True))
    op.create_index("idx_dk_chunks_partition", "domain_knowledge_chunks", ["partition_id"])


def downgrade() -> None:
    op.drop_index("idx_dk_chunks_partition", table_name="domain_knowledge_chunks")
    op.drop_column("domain_knowledge_chunks", "partition_id")
