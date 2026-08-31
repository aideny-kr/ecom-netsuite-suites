"""094 — user_dashboard_preferences gains series_id (rolling-period Stage 1, Task 4).

A dashboard preference selects EITHER a report (a pinned snapshot) OR a series (a
tracking selection that follows the series' newest report) — never both. `series_id`
mirrors `report_id`'s own shape from migration 092: nullable, FK -> report_series.id
ON DELETE SET NULL (deleting a series tombstones the preference to NULL instead of
deleting the row, so the read side can self-heal to the most-recently-published report
with a fallback flag — same story 092 already tells for a deleted report).

The CHECK constraint is the load-bearing new piece: `report_id IS NULL OR series_id IS
NULL` allows both NULL (a fully tombstoned row, or a never-set default) and exactly one
set, but rejects both being set simultaneously at the DB level — the API's own 400
("exactly one of report_id or series_id") is belt; this is suspenders.

Every existing row has `series_id` NULL already (a new column), so the CHECK is
trivially satisfied by pre-094 data — no backfill.

Verify single head before/after (alembic heads); never a merge migration.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "094_dashboard_preference_series"
down_revision = "093_report_series"  # current single head (verify: alembic heads)
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_dashboard_preferences",
        sa.Column(
            "series_id",
            UUID(as_uuid=True),
            sa.ForeignKey("report_series.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_user_dashboard_preferences_series_id", "user_dashboard_preferences", ["series_id"])
    op.create_check_constraint(
        "ck_user_dashboard_preference_one_selection",
        "user_dashboard_preferences",
        "report_id IS NULL OR series_id IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_user_dashboard_preference_one_selection", "user_dashboard_preferences", type_="check")
    op.drop_index("ix_user_dashboard_preferences_series_id", table_name="user_dashboard_preferences")
    op.drop_column("user_dashboard_preferences", "series_id")
