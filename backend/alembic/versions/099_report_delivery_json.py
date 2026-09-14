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
# Re-parented 2026-09-10 from 098_celigo_flow_errors_checked onto the transaction-ops
# lineage head (PR #225): staging had 099_transaction_ops_state … 107_tx_settlement_queue
# applied while this chain (099 → 102_schedule_retry_json) was applied on no real DB, so the
# scheduled-jobs chain is the one that moves. One linear history — never a merge revision,
# because the deploy migration-safety check runs `downgrade -1` from head and a merge node
# fails it with "Ambiguous walk" (memory: feedback_merge_migration_breaks_downgrade).
down_revision = "107_tx_settlement_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reports", sa.Column("delivery_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("reports", "delivery_json")
