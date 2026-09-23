"""Index the NetSuite records behind saved reconciliation observations."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "112_transaction_dependencies"
down_revision = "111_transaction_source_snapshot"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "transaction_netsuite_dependencies",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("order_reference", sa.String(100), nullable=False),
        sa.Column("connection_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.String(255), nullable=False),
        sa.Column("record_type", sa.String(64), nullable=False),
        sa.Column("record_id", sa.String(30), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "order_reference"],
            [
                "transaction_ops_findings.tenant_id",
                "transaction_ops_findings.run_id",
                "transaction_ops_findings.order_reference",
            ],
            ondelete="CASCADE",
            name="fk_tx_ns_dependency_finding",
        ),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "order_reference", "record_type", "record_id", name="uq_tx_ns_dependency"
        ),
    )
    op.create_index(
        "ix_tx_ns_dependency_record",
        "transaction_netsuite_dependencies",
        ["tenant_id", "connection_id", "account_id", "record_type", "record_id"],
    )
    op.execute("ALTER TABLE transaction_netsuite_dependencies ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE transaction_netsuite_dependencies FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON transaction_netsuite_dependencies "
        "USING (tenant_id = get_current_tenant_id()) WITH CHECK (tenant_id = get_current_tenant_id())"
    )


def downgrade():
    op.drop_table("transaction_netsuite_dependencies")
