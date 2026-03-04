"""Add branding and custom domain columns to tenant_configs."""

from alembic import op
import sqlalchemy as sa

revision = "036_tenant_branding_and_domains"
down_revision = "035_tenant_unified_agent_flag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Branding
    op.add_column("tenant_configs", sa.Column("brand_name", sa.String(100), nullable=True))
    op.add_column("tenant_configs", sa.Column("brand_color_hsl", sa.String(30), nullable=True))
    op.add_column("tenant_configs", sa.Column("brand_logo_url", sa.Text(), nullable=True))
    op.add_column("tenant_configs", sa.Column("brand_favicon_url", sa.Text(), nullable=True))

    # Custom domain mapping
    op.add_column("tenant_configs", sa.Column("custom_domain", sa.String(255), nullable=True))
    op.add_column(
        "tenant_configs",
        sa.Column("domain_verified", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_unique_constraint("uq_tenant_configs_custom_domain", "tenant_configs", ["custom_domain"])


def downgrade() -> None:
    op.drop_constraint("uq_tenant_configs_custom_domain", "tenant_configs", type_="unique")
    op.drop_column("tenant_configs", "domain_verified")
    op.drop_column("tenant_configs", "custom_domain")
    op.drop_column("tenant_configs", "brand_favicon_url")
    op.drop_column("tenant_configs", "brand_logo_url")
    op.drop_column("tenant_configs", "brand_color_hsl")
    op.drop_column("tenant_configs", "brand_name")
