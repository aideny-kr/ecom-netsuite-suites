"""041_user_feedback"""

from alembic import op
import sqlalchemy as sa

revision = "041_user_feedback"
down_revision = "040_query_importance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("user_feedback", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "user_feedback")
