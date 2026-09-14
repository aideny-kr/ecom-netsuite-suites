"""schedules -- Scheduled Jobs platform columns (Slice 2, Task 1)

Revision ID: 100_scheduled_jobs
Revises: 099_report_delivery_json

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
§B1. Extends the pre-existing per-tenant `schedules` table (unchanged: id,
tenant_id, name, schedule_type, cron_expression, is_active, parameters,
created_at, updated_at) with the columns a Scheduled Job needs: the
plain-language `instruction` (source of truth), the compiled `plan_json` that
actually runs plus its `plan_version`/`plan_status`, a `pending_plan_json` for
an instruction edit awaiting approval, delivery/budget/catch-up policy, owner,
and the last/next run bookkeeping the list page and the Beat sweep both read.

No CHECK constraints on the enum-shaped text columns (plan_status, catch_up) --
this repo's existing enum-shaped `schedules.schedule_type` and `jobs.status`
columns are plain TEXT/VARCHAR validated at the application layer (Pydantic),
not the database layer; this migration follows that established convention
rather than introducing a new one for just these two columns.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "100_scheduled_jobs"
down_revision = "099_report_delivery_json"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("schedules", sa.Column("instruction", sa.Text(), nullable=True))
    op.add_column("schedules", sa.Column("plan_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("schedules", sa.Column("plan_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("schedules", sa.Column("plan_status", sa.Text(), nullable=True))
    op.add_column("schedules", sa.Column("pending_plan_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("schedules", sa.Column("pending_plan_reason", sa.Text(), nullable=True))
    op.add_column("schedules", sa.Column("timezone", sa.Text(), nullable=False, server_default="UTC"))
    op.add_column("schedules", sa.Column("delivery_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("schedules", sa.Column("budget_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("schedules", sa.Column("catch_up", sa.Text(), nullable=False, server_default="once"))
    op.add_column(
        "schedules",
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("schedules", sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("schedules", sa.Column("last_run_status", sa.Text(), nullable=True))
    op.add_column("schedules", sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("schedules", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("schedules", sa.Column("pause_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("schedules", "pause_reason")
    op.drop_column("schedules", "paused_at")
    op.drop_column("schedules", "next_run_at")
    op.drop_column("schedules", "last_run_status")
    op.drop_column("schedules", "last_run_at")
    op.drop_column("schedules", "owner_id")
    op.drop_column("schedules", "catch_up")
    op.drop_column("schedules", "budget_json")
    op.drop_column("schedules", "delivery_json")
    op.drop_column("schedules", "timezone")
    op.drop_column("schedules", "pending_plan_reason")
    op.drop_column("schedules", "pending_plan_json")
    op.drop_column("schedules", "plan_status")
    op.drop_column("schedules", "plan_version")
    op.drop_column("schedules", "plan_json")
    op.drop_column("schedules", "instruction")
