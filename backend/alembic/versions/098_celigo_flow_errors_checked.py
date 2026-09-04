"""celigo_flows.errors_checked_at -- per-flow error-summary freshness cursor

Revision ID: 098_celigo_flow_errors_checked
Revises: 097_celigo_flow_step_ref_name

Stamped by `sync_service.py`'s Phase E once per flow, after that flow's own
step loop, every time `client.list_flow_error_summary` (verified live
2026-09-03) was actually consulted for it -- independent of whether any step
had a non-zero count. NULL means never checked with the correct endpoint, so
a zero open-error count shown anywhere in the API is not a verified zero
until this is set. Same file shape and ruff import order as 097.
"""

import sqlalchemy as sa

from alembic import op

revision = "098_celigo_flow_errors_checked"
down_revision = "097_celigo_flow_step_ref_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("celigo_flows", sa.Column("errors_checked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("celigo_flows", "errors_checked_at")
