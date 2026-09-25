"""Allow immutable candidate-discovery batches; financial batch age stays unchanged."""

from alembic import op

revision = "114_dependency_batches"
down_revision = "113_transaction_evidence_batches"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_tx_evidence_batch_kind", "transaction_evidence_batches", type_="check")
    op.create_check_constraint(
        "ck_tx_evidence_batch_kind", "transaction_evidence_batches", "kind IN ('orders','refunds','dependencies')"
    )


def downgrade():
    # Do not silently destroy pending checkpoints on downgrade. Existing dependency
    # rows deliberately make this fail; application rollback uses additive schema.
    op.drop_constraint("ck_tx_evidence_batch_kind", "transaction_evidence_batches", type_="check")
    op.create_check_constraint(
        "ck_tx_evidence_batch_kind", "transaction_evidence_batches", "kind IN ('orders','refunds')"
    )
