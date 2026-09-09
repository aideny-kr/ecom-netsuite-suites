"""reports.delivery_json -- Drive delivery receipt (Task 5, Slice 1 §A5)

Revision ID: 099_report_delivery_json
Revises: 098_celigo_flow_errors_checked

Written ONLY by a successful deliver_report_to_drive() call, atomically alongside
the pre-existing published_drive_url/published_at columns:
{pdf: {file_id, url}, xlsx: {file_id, url}, folder_id, period_key, delivered_at}.
NULL = never delivered, or the last delivery attempt failed (report_delivery.py never
writes a partial value here on failure -- see its own module docstring).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "099_report_delivery_json"
down_revision = "098_celigo_flow_errors_checked"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reports", sa.Column("delivery_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("reports", "delivery_json")
