"""Preserve immutable reconciliation configuration history when binding a replica."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "105_tx_config_revisions"
down_revision = "104_tx_run_queue_time"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "transaction_ops_configs", sa.Column("supersedes_config_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_tx_config_revision",
        "transaction_ops_configs",
        "transaction_ops_configs",
        ["tenant_id", "supersedes_config_id"],
        ["tenant_id", "id"],
    )
    op.create_unique_constraint(
        "uq_tx_config_revision", "transaction_ops_configs", ["tenant_id", "supersedes_config_id"]
    )
    op.create_check_constraint(
        "ck_tx_config_revision", "transaction_ops_configs", "supersedes_config_id IS NULL OR supersedes_config_id <> id"
    )


def downgrade():
    op.drop_constraint("ck_tx_config_revision", "transaction_ops_configs", type_="check")
    op.drop_constraint("uq_tx_config_revision", "transaction_ops_configs", type_="unique")
    op.drop_constraint("fk_tx_config_revision", "transaction_ops_configs", type_="foreignkey")
    op.drop_column("transaction_ops_configs", "supersedes_config_id")
