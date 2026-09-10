"""Identify bounded, internal read-only outcome-recovery runs.

Revision ID: 101_tx_recovery_runs
Revises: 100_tx_operation_budgets
"""

from alembic import op

revision = "101_tx_recovery_runs"
down_revision = "100_tx_operation_budgets"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_tx_run_origin", "transaction_ops_runs", type_="check")
    op.create_check_constraint(
        "ck_tx_run_origin", "transaction_ops_runs", "origin IN ('manual','chat','schedule','recovery')"
    )


def downgrade():
    # Preserve audit evidence: existing recovery runs make this downgrade fail
    # transactionally, rather than deleting or relabelling those records.
    op.drop_constraint("ck_tx_run_origin", "transaction_ops_runs", type_="check")
    op.create_check_constraint("ck_tx_run_origin", "transaction_ops_runs", "origin IN ('manual','chat','schedule')")
