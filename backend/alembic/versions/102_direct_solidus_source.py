"""Allow one direct Solidus connection instead of a Celigo source step.

Revision ID: 102_direct_solidus_source
Revises: 101_tx_recovery_runs
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "102_direct_solidus_source"
down_revision = "101_tx_recovery_runs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "transaction_ops_configs", sa.Column("source_connection_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_tx_source_connection", "transaction_ops_configs", "connections", ["source_connection_id"], ["id"]
    )
    op.alter_column("transaction_ops_configs", "source_step_id", nullable=True)
    op.create_check_constraint(
        "ck_tx_config_source",
        "transaction_ops_configs",
        "(source_step_id IS NOT NULL) <> (source_connection_id IS NOT NULL)",
    )


def downgrade():
    # Refuse to discard direct-source scopes or their audit history.
    op.alter_column("transaction_ops_configs", "source_step_id", nullable=False)
    op.drop_constraint("ck_tx_config_source", "transaction_ops_configs", type_="check")
    op.drop_constraint("fk_tx_source_connection", "transaction_ops_configs", type_="foreignkey")
    op.drop_column("transaction_ops_configs", "source_connection_id")
