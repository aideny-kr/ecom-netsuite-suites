"""Durable chat submission receipts for retry-safe clients.

Apply before deploying clients that send request_id. Existing clients remain valid.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "108_chat_submissions"
down_revision = "102_schedule_retry_job"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_submissions",
        sa.Column(
            "session_id", UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("request_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chat_submissions_tenant_id", "chat_submissions", ["tenant_id"])
    op.execute("ALTER TABLE chat_submissions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chat_submissions FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY chat_submissions_tenant_isolation ON chat_submissions
        USING (tenant_id = get_current_tenant_id())
        WITH CHECK (tenant_id = get_current_tenant_id())
    """)


def downgrade() -> None:
    op.drop_table("chat_submissions")
