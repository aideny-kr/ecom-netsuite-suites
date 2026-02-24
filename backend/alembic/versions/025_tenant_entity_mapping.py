"""025_tenant_entity_mapping

Create tenant_entity_mapping table with pg_trgm and btree_gin extensions
for high-speed fuzzy entity resolution per tenant.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "025_tenant_entity_mapping"
down_revision = "024_add_custom_record_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Enable extensions BEFORE any table or index creation
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gin")

    # 2. Create the table
    op.create_table(
        "tenant_entity_mapping",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("natural_name", sa.String(255), nullable=False),
        sa.Column("script_id", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # 3. Unique constraint for upsert: one mapping per (tenant, entity_type, script_id)
    op.create_unique_constraint(
        "uq_tenant_entity_type_script",
        "tenant_entity_mapping",
        ["tenant_id", "entity_type", "script_id"],
    )

    # 4. Composite GIN index: strict tenant_id equality (btree_gin) + fuzzy name matching (gin_trgm_ops)
    op.execute("""
        CREATE INDEX idx_tenant_entity_trgm
        ON tenant_entity_mapping
        USING GIN (tenant_id, natural_name gin_trgm_ops)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_tenant_entity_trgm")
    op.drop_table("tenant_entity_mapping")
