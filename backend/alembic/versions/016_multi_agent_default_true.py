"""Change multi_agent_enabled default to true and flip existing rows.

Revision ID: 016_multi_agent_default_true
Revises: 015_add_netsuite_metadata
"""

from alembic import op

revision = "016_multi_agent_default_true"
down_revision = "015_add_netsuite_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Flip existing tenants to multi-agent enabled
    op.execute("UPDATE tenant_configs SET multi_agent_enabled = true")
    # Change the server default for new rows
    op.alter_column(
        "tenant_configs",
        "multi_agent_enabled",
        server_default="true",
    )


def downgrade() -> None:
    op.alter_column(
        "tenant_configs",
        "multi_agent_enabled",
        server_default="false",
    )
    op.execute("UPDATE tenant_configs SET multi_agent_enabled = false")
