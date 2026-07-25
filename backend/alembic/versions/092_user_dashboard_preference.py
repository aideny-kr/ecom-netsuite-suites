"""092 — user_dashboard_preferences table + RLS + FORCE RLS.

Wallpaper Task 1: the dashboard landing page shows ONE published report at
full size, chosen per user (the *published set* — reports.dashboard_pinned_at
from migration 091 — stays workspace-wide; the *active choice* is personal).

One row per (tenant, user): the report they last selected as their wallpaper.
`report_id` cascades on report delete so an unpublished-then-deleted report
simply drops the selection and the user falls back to the most recently
published report (Task 2's read-side logic) — no orphaned FK, no dangling
selection to clean up here.

RLS mirrors 084 (reports): ENABLE + FORCE, USING/WITH CHECK both pinned to
get_current_tenant_id(), no OR-SYSTEM branch — these rows are never
SYSTEM-owned, only ever a specific user's personal choice.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "092_user_dashboard_preference"
down_revision = "091_report_dashboard_pin"  # current single head (verify: alembic heads)
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_dashboard_preferences",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("report_id", UUID(as_uuid=True), sa.ForeignKey("reports.id", ondelete="CASCADE"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_user_dashboard_preference_tenant_user"),
    )
    op.create_index("ix_user_dashboard_preferences_tenant_id", "user_dashboard_preferences", ["tenant_id"])
    op.create_index("ix_user_dashboard_preferences_user_id", "user_dashboard_preferences", ["user_id"])

    # RLS — NO OR-SYSTEM branch (preference rows are never SYSTEM-owned).
    op.execute("ALTER TABLE user_dashboard_preferences ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY user_dashboard_preferences_tenant_isolation ON user_dashboard_preferences
        USING (tenant_id = get_current_tenant_id())
        WITH CHECK (tenant_id = get_current_tenant_id())
    """)
    op.execute(
        "ALTER TABLE user_dashboard_preferences FORCE ROW LEVEL SECURITY"
    )  # load-bearing on Supabase (owner != BYPASSRLS)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS user_dashboard_preferences_tenant_isolation ON user_dashboard_preferences")
    op.drop_table("user_dashboard_preferences")
