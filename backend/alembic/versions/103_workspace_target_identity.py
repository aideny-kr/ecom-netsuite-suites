"""Pin validation identity in workspace deployment confirmations.

Existing previews deliberately remain unbound and must be recreated.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "103_workspace_target_identity"
down_revision = "102_schedule_retry_job"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspace_deploy_tokens", sa.Column("target_identity", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("workspace_deploy_tokens", "target_identity")
