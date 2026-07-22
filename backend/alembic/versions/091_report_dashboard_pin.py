"""091 — reports.dashboard_pinned_at (reports UX trio, Task 2).

Pin-to-dashboard: a creator/admin can pin a report to the dashboard landing
page. NULL = not pinned; set to the pin time when pinned, cleared on unpin.
Re-pinning bumps it forward — the dashboard sorts pinned reports newest-first
by this column. Additive, nullable; no backfill needed.

RLS: a column on reports — 084's ENABLE + FORCE tenant-isolation policy
applies to it automatically; no new RLS statements needed.
"""

import sqlalchemy as sa

from alembic import op

revision = "091_report_dashboard_pin"
down_revision = "090_netsuite_currency_truth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reports",
        sa.Column("dashboard_pinned_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("reports", "dashboard_pinned_at")
