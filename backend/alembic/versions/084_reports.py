"""reports table + RLS + FORCE RLS

083 is intentionally unused — the dropped merge migration. Alembic revision ids are
arbitrary strings, so 084 chaining off 082_metric_def_with_check is valid (current
single head). reports rows are NEVER SYSTEM-owned, so the policy has NO OR-SYSTEM branch:
both USING and WITH CHECK pin every read/write to the caller's own active tenant context.
FORCE ROW LEVEL SECURITY is load-bearing on Supabase (the table-owning app role is NOT
BYPASSRLS), exactly as 081/082 needed for metric_definitions.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "084_reports"
down_revision = "082_metric_def_with_check"  # current single head (verify: alembic heads)
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("spec_json", JSONB(), nullable=False),
        sa.Column("rendered_html", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("source_run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("published_drive_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_reports_tenant", "reports", ["tenant_id"])
    # RLS — NO OR-SYSTEM branch (reports are never SYSTEM-owned).
    op.execute("ALTER TABLE reports ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY reports_tenant_isolation ON reports
        USING (tenant_id = get_current_tenant_id())
        WITH CHECK (tenant_id = get_current_tenant_id())
    """)
    op.execute("ALTER TABLE reports FORCE ROW LEVEL SECURITY")  # load-bearing on Supabase (owner != BYPASSRLS)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS reports_tenant_isolation ON reports")
    op.drop_table("reports")
