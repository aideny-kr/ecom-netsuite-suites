"""schedules.created_via -- Scheduled Jobs platform (Slice 2, Task 5 residual)

Revision ID: 101_schedule_created_via
Revises: 100_scheduled_jobs

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
§B6. The list page's Job column sub-line ("from the chat · owner {name}" /
"owner {name}", mock state one) needs to know HOW a schedule was created --
`"chat"` (the agent's MCP `schedule.create` tool call), `"page"` (this page's
own New Job wizard, `POST /schedules`), or `"seed"` (an admin/seed script).
No CHECK constraint, matching this migration's own `plan_status`/`catch_up`
precedent (see 100_scheduled_jobs's docstring) -- validated at the
application layer, not the database layer.
"""

import sqlalchemy as sa

from alembic import op

revision = "101_schedule_created_via"
down_revision = "100_scheduled_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("schedules", sa.Column("created_via", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("schedules", "created_via")
