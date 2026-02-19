"""Add netsuite_api_logs table for request/response logging."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "018_netsuite_api_log"
down_revision = "017_suitescript_sync"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "netsuite_api_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("request_headers", postgresql.JSON, nullable=True),
        sa.Column("request_body", sa.Text, nullable=True),
        sa.Column("response_status", sa.Integer, nullable=True),
        sa.Column("response_body", sa.Text, nullable=True),
        sa.Column("response_time_ms", sa.Integer, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("source", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_netsuite_api_logs_tenant_created",
        "netsuite_api_logs",
        ["tenant_id", "created_at"],
    )


def downgrade():
    op.drop_index("ix_netsuite_api_logs_tenant_created")
    op.drop_table("netsuite_api_logs")
