"""Add fiscal_year_start_month to tenant_configs.

Enables agents to interpret Q1/Q2/Q3/Q4/fiscal year queries using the
tenant's actual fiscal calendar instead of defaulting to calendar year.
"""

import sqlalchemy as sa

from alembic import op

revision = "065_fiscal_year_start"
down_revision = "064_source_pin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_configs",
        sa.Column(
            "fiscal_year_start_month",
            sa.SmallInteger,
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant_configs", "fiscal_year_start_month")
