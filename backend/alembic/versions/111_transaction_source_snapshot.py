"""Retain sanitized Solidus detail reads without changing financial evidence or budgets."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "111_transaction_source_snapshot"
down_revision = "110_run_call_holds"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_orders_source_reference", "orders", ["tenant_id", "source_connection_id", "order_number"])
    op.create_table(
        "transaction_source_snapshots",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "connection_id", pg.UUID(as_uuid=True), sa.ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("order_reference", sa.String(100), nullable=False),
        sa.Column("connection_fingerprint", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_json", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "connection_id", "order_reference", name="uq_tx_source_snapshot"),
    )
    op.create_index("ix_transaction_source_snapshots_tenant_id", "transaction_source_snapshots", ["tenant_id"])
    op.execute("ALTER TABLE transaction_source_snapshots ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE transaction_source_snapshots FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON transaction_source_snapshots "
        "USING (tenant_id = get_current_tenant_id()) WITH CHECK (tenant_id = get_current_tenant_id())"
    )


def downgrade():
    op.drop_table("transaction_source_snapshots")
    op.drop_index("ix_orders_source_reference", table_name="orders")
