"""024_add_custom_record_fields

Add column to store discovered fields for each custom record type.
"""

from alembic import op
import sqlalchemy as sa

revision = "024_add_custom_record_fields"
down_revision = "023_audit_uuidv7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "netsuite_metadata",
        sa.Column("custom_record_fields", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("netsuite_metadata", "custom_record_fields")
