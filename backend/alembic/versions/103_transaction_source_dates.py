"""Preserve source order dates and unknown or higher-precision amounts."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "103_transaction_source_dates"
down_revision = "102_direct_solidus_source"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("total_amount", "subtotal", "tax_amount", "discount_amount"):
        op.alter_column("orders", name, type_=sa.Numeric(24, 6), nullable=name != "total_amount")
    op.add_column("orders", sa.Column("source_connection_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_orders_source_connection", "orders", "connections", ["source_connection_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_orders_source_connection_id", "orders", ["source_connection_id"])
    for name in ("source_created_at", "source_updated_at"):
        op.add_column("orders", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_orders_tenant_source_date", "orders", ["tenant_id", "source_created_at"])


def downgrade():
    # Refuse a downgrade that would round amounts or replace unknowns with zero.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM orders WHERE subtotal IS NULL OR tax_amount IS NULL
          OR discount_amount IS NULL OR total_amount <> round(total_amount, 2)
          OR subtotal <> round(subtotal, 2) OR tax_amount <> round(tax_amount, 2)
          OR discount_amount <> round(discount_amount, 2)) THEN
            RAISE EXCEPTION 'Order evidence cannot be represented by the previous schema';
        END IF;
    END $$""")
    op.drop_index("ix_orders_tenant_source_date", table_name="orders")
    op.drop_index("ix_orders_source_connection_id", table_name="orders")
    op.drop_constraint("fk_orders_source_connection", "orders", type_="foreignkey")
    for name in ("source_created_at", "source_updated_at", "source_connection_id"):
        op.drop_column("orders", name)
    for name in ("total_amount", "subtotal", "tax_amount", "discount_amount"):
        op.alter_column("orders", name, type_=sa.Numeric(15, 2), nullable=False)
