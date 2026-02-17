"""Add BYOK AI provider columns to tenant_configs and chat_messages

Revision ID: 004_ai_provider_byok
Revises: 003_mcp_connectors
Create Date: 2026-02-16
"""

import sqlalchemy as sa

from alembic import op

revision = "004_ai_provider_byok"
down_revision = "003_mcp_connectors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add AI provider columns to tenant_configs
    op.add_column("tenant_configs", sa.Column("ai_provider", sa.String(20), nullable=True))
    op.add_column("tenant_configs", sa.Column("ai_model", sa.String(100), nullable=True))
    op.add_column("tenant_configs", sa.Column("ai_api_key_encrypted", sa.Text, nullable=True))
    op.add_column("tenant_configs", sa.Column("ai_key_version", sa.Integer, server_default="1", nullable=False))

    # Add token tracking columns to chat_messages
    op.add_column("chat_messages", sa.Column("input_tokens", sa.Integer, nullable=True))
    op.add_column("chat_messages", sa.Column("output_tokens", sa.Integer, nullable=True))
    op.add_column("chat_messages", sa.Column("model_used", sa.String(100), nullable=True))
    op.add_column("chat_messages", sa.Column("provider_used", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "provider_used")
    op.drop_column("chat_messages", "model_used")
    op.drop_column("chat_messages", "output_tokens")
    op.drop_column("chat_messages", "input_tokens")

    op.drop_column("tenant_configs", "ai_key_version")
    op.drop_column("tenant_configs", "ai_api_key_encrypted")
    op.drop_column("tenant_configs", "ai_model")
    op.drop_column("tenant_configs", "ai_provider")
