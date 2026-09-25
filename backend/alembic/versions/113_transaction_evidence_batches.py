"""Durable bounded native reconciliation batches, separate from coverage claims."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "113_transaction_evidence_batches"
down_revision = "112_transaction_dependencies"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "transaction_evidence_batches",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("connection_fingerprint", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_json", pg.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"], ["transaction_ops_runs.tenant_id", "transaction_ops_runs.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("kind IN ('orders','refunds')", name="ck_tx_evidence_batch_kind"),
        sa.CheckConstraint("completed_at >= started_at", name="ck_tx_evidence_batch_times"),
    )
    op.create_index("ix_transaction_evidence_batches_tenant_id", "transaction_evidence_batches", ["tenant_id"])
    op.execute("ALTER TABLE transaction_evidence_batches ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE transaction_evidence_batches FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON transaction_evidence_batches "
        "USING (tenant_id = get_current_tenant_id()) WITH CHECK (tenant_id = get_current_tenant_id())"
    )


def downgrade():
    op.drop_table("transaction_evidence_batches")
