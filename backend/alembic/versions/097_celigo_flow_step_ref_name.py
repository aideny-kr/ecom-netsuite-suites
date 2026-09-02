"""celigo_flow_steps.reference_name -- Celigo's own export/import name on each step

Revision ID: 097_celigo_flow_step_ref_name
Revises: 094_dashboard_preference_series

NOTE: the task brief's literal revision id
("097_celigo_flow_step_reference_name", 35 chars) overflows this repo's
`alembic_version.version_num` -- `varchar(32)` -- and crashes the upgrade
with `StringDataRightTruncationError` (confirmed by execution: sibling
096_celigo_flow_step_provenance is 31 chars, already right at the edge).
Shortened to `097_celigo_flow_step_ref_name` (29 chars), same meaning.
"""

import sqlalchemy as sa
from alembic import op

revision = "097_celigo_flow_step_ref_name"
down_revision = "094_dashboard_preference_series"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("celigo_flow_steps", sa.Column("reference_name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("celigo_flow_steps", "reference_name")
