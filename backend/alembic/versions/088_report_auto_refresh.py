"""088 — reports.auto_refresh interval + failure-ladder state (Slice C).

Slice C of live-dashboard reports (dashboard mode): per-report auto-refresh interval
(off | hourly | daily) swept by a Beat task. Default 'daily' (§6.1 product decision)
— legacy/snapshot rows also read 'daily' but are inert: the sweep predicate requires
recipe_json IS NOT NULL, so a report without a captured recipe is never refreshed.
No CHECK constraint on the interval (house convention — reports.status is likewise
convention-only); validity is enforced at the API schema boundary.

Failure ladder (launch-critical with daily-by-default, §4C/§6.1):
refresh_failure_count counts CONSECUTIVE failed auto-refreshes (sweep-owned; reset to
0 on success); auto_refresh_paused_at is stamped when the ladder pauses a report
(~7 consecutive failures) and cleared ONLY by the user's explicit resume — never by
a later success, otherwise one failure after reconnect would re-pause instantly.

RLS: columns on reports — 084's ENABLE + FORCE tenant-isolation policy applies to
them automatically; no new RLS statements needed.

Spec: docs/superpowers/specs/2026-07-02-live-dashboard-reports.md §4C/§6
"""

import sqlalchemy as sa

from alembic import op

revision = "088_report_auto_refresh"
down_revision = "087_report_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reports",
        sa.Column("auto_refresh", sa.Text(), nullable=False, server_default="daily"),
    )
    op.add_column(
        "reports",
        sa.Column("refresh_failure_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "reports",
        sa.Column("auto_refresh_paused_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("reports", "auto_refresh_paused_at")
    op.drop_column("reports", "refresh_failure_count")
    op.drop_column("reports", "auto_refresh")
