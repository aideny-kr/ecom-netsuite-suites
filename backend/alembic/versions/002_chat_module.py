"""Chat module: chat_sessions, chat_messages, doc_chunks with pgvector

Revision ID: 002_chat_module
Revises: 001_initial
Create Date: 2026-02-16
"""

import uuid

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSON, UUID

from alembic import op

revision = "002_chat_module"
down_revision = "001_initial"
branch_labels = None
depends_on = None

CHAT_RLS_TABLES = ["chat_sessions", "chat_messages"]


def upgrade() -> None:
    # Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ---- Chat Sessions ----
    op.create_table(
        "chat_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("is_archived", sa.Boolean, server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_chat_sessions_tenant_id", "chat_sessions", ["tenant_id"])
    op.create_index("ix_chat_sessions_user_id", "chat_sessions", ["user_id"])

    # ---- Chat Messages ----
    op.create_table(
        "chat_messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("tool_calls", JSON, nullable=True),
        sa.Column("citations", JSON, nullable=True),
        sa.Column("token_count", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])

    # ---- Doc Chunks ----
    op.create_table(
        "doc_chunks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_path", sa.String(512), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("token_count", sa.Integer, nullable=False),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("metadata", JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_doc_chunks_tenant_id", "doc_chunks", ["tenant_id"])
    op.create_index("ix_doc_chunks_tenant_source", "doc_chunks", ["tenant_id", "source_path"])

    # HNSW index for vector similarity search
    op.execute("CREATE INDEX ix_doc_chunks_embedding ON doc_chunks USING hnsw (embedding vector_cosine_ops)")

    # ---- RLS Policies ----
    for table in CHAT_RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
        """)

    # doc_chunks RLS includes system tenant for shared docs
    op.execute("ALTER TABLE doc_chunks ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY doc_chunks_tenant_isolation ON doc_chunks
        USING (
            tenant_id = current_setting('app.current_tenant_id')::uuid
            OR tenant_id = '00000000-0000-0000-0000-000000000000'::uuid
        )
    """)


def downgrade() -> None:
    # Drop RLS policies
    for table in CHAT_RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS doc_chunks_tenant_isolation ON doc_chunks")
    op.execute("ALTER TABLE doc_chunks DISABLE ROW LEVEL SECURITY")

    # Drop HNSW index
    op.execute("DROP INDEX IF EXISTS ix_doc_chunks_embedding")

    # Drop tables
    op.drop_table("doc_chunks")
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
