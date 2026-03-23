"""Agent configuration table for per-tenant agent overrides and metrics."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "051_agent_configs"
down_revision = "050_ws_script_type"


def upgrade() -> None:
    op.create_table(
        "agent_configs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("override_config", JSONB, nullable=True, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_latency_ms", sa.Float(), nullable=True),
        sa.Column("avg_cost", sa.Float(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "agent_id", name="uq_agent_configs_tenant_agent"),
    )
    op.create_index(
        "idx_agent_configs_tenant_enabled",
        "agent_configs",
        ["tenant_id", "is_enabled"],
        postgresql_where=sa.text("is_enabled = true"),
    )


def downgrade() -> None:
    op.drop_index("idx_agent_configs_tenant_enabled", table_name="agent_configs")
    op.drop_table("agent_configs")
