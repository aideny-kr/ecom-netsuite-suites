"""metric_definitions hardening: FORCE ROW LEVEL SECURITY"""

from alembic import op

revision = "081_metric_definitions_hardening"
down_revision = "080_metric_definitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # R3#27: FORCE RLS so the policy is enforced even for the table owner (postgres
    # superuser). Without FORCE the owner bypasses RLS entirely — making the policy
    # vacuous for the service role that owns the table. With FORCE, every session
    # must satisfy the USING clause regardless of role, closing the owner bypass gap.
    op.execute("ALTER TABLE metric_definitions FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE metric_definitions NO FORCE ROW LEVEL SECURITY")
