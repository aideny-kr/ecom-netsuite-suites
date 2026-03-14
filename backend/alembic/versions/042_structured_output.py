"""Add structured_output JSONB column to chat_messages for persisting
financial report and data table payloads across page refreshes."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

revision = "042_structured_output"
down_revision = "041_user_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("structured_output", JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "structured_output")
