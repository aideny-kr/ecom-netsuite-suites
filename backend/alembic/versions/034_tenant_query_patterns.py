"""Tenant query patterns — cross-session learning for SuiteQL.

Stores successful SuiteQL queries per tenant with vector embeddings
for semantic retrieval of proven patterns.

Revision ID: 034_tenant_query_patterns
Revises: 033_connection_health_fields
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from pgvector.sqlalchemy import Vector

revision = "034_tenant_query_patterns"
down_revision = "033_connection_health_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_query_patterns",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("intent_embedding", Vector(1536), nullable=True),
        sa.Column("user_question", sa.Text(), nullable=False),
        sa.Column("working_sql", sa.Text(), nullable=False),
        sa.Column("tables_used", ARRAY(sa.Text()), nullable=True),
        sa.Column("columns_used", ARRAY(sa.Text()), nullable=True),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("tenant_id", "working_sql", name="uq_tenant_query_pattern"),
    )
    # HNSW index for fast vector similarity search
    op.execute(
        "CREATE INDEX ix_tenant_query_patterns_embedding ON tenant_query_patterns "
        "USING hnsw (intent_embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_tenant_query_patterns_embedding")
    op.drop_table("tenant_query_patterns")
