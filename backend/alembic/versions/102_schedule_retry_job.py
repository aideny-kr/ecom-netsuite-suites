"""schedules.retry_job_id -- Scheduled Jobs platform (Slice 2, delta gate item 1)

Revision ID: 102_schedule_retry_job
Revises: 101_schedule_created_via

Delta T2 gate over the previous fix round confirmed four majors that all
share one root cause: the one 15-minutes-later retry (spec §B4) was
attributed via a JSON query on `jobs` (`Job.parameters["attempt"].astext ==
"1"` ORDER BY `started_at` DESC) instead of a real column. That query broke
in three ways at once -- a BLOCKED attempt-1 row with `started_at IS NULL`
sorts FIRST under `DESC` (Postgres: NULLS FIRST for DESC), a manual "Run
now" also creates an `attempt=1` row the query could not tell apart from the
sweep's own, and a manual failure overwrites `last_run_status` (which the
claim ALSO read to decide "is this the retry?"), resetting the attempt
counter. The fix is the mechanism, not the query: `schedules.retry_job_id`
points directly at the pre-created `jobs` row for the pending retry (set at
scheduling time, cleared the moment the sweep claims it) -- an explicit,
unambiguous reference no amount of noise from other rows or fields can
confuse.

`ON DELETE SET NULL` (not RESTRICT/CASCADE): a `jobs` row can be purged
independently of the schedule that produced it (retention, an admin
cleanup) -- the schedule must not be blocked from being deleted, or worse,
have its own row cascade-deleted, just because a retry `jobs` row it once
pointed at is gone. A schedule left with a dangling reference silently
"loses" the retry rather than erroring.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "102_schedule_retry_job"
down_revision = "101_schedule_created_via"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "schedules",
        sa.Column(
            "retry_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("schedules", "retry_job_id")
