"""087 — report_versions (immutable snapshots) + reports.last_refreshed_at (Slice B).

Slice B of live-dashboard reports (manual refresh): the parent reports row stays the
stable identity/URL and MIRRORS the current version's spec_json/rendered_html/version;
report_versions holds one IMMUTABLE row per published version. Version 1 is
lazy-snapshotted from the parent on a report's FIRST refresh (no backfill; a
never-refreshed report has zero child rows and the picker derives "v1 - current" from
the parent). (report_id, version) is unique; rows are insert-only — deliberately NO
updated_at column (an onupdate stamp on an immutable row would be a lie). `pinned`
ships now (dormant in B) so Slice C's pinned-exempt retention needs no second
migration. reports.last_refreshed_at is the DB-derived refresh debounce stamp
(attempt-time, ~5 min window) — no new infra, works across replicas/restarts.

RLS: report_versions rows are NEVER SYSTEM-owned — same policy shape as 084_reports
(no OR-SYSTEM branch), ENABLE + FORCE. FORCE is load-bearing on Supabase (the
table-owning app role is NOT BYPASSRLS).

Spec: docs/superpowers/specs/2026-07-02-live-dashboard-reports.md §4B/§6
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "087_report_versions"
down_revision = "086_report_recipe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "report_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec_json", postgresql.JSONB(), nullable=False),
        sa.Column("rendered_html", sa.Text(), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default="false"),  # Slice-C forward-compat
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("report_id", "version", name="uq_report_versions_report_version"),
    )
    op.execute("ALTER TABLE report_versions ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY report_versions_tenant_isolation ON report_versions
        USING (tenant_id = get_current_tenant_id())
        WITH CHECK (tenant_id = get_current_tenant_id())
        """
    )
    op.execute("ALTER TABLE report_versions FORCE ROW LEVEL SECURITY")
    op.add_column("reports", sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("reports", "last_refreshed_at")
    op.execute("DROP POLICY IF EXISTS report_versions_tenant_isolation ON report_versions")
    op.drop_table("report_versions")
