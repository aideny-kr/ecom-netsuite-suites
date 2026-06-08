"""Enable row-level security on tenant_learned_rules.

Revision ID: 080_learned_rules_rls
Revises: 079_order_ref_pattern
Create Date: 2026-06-05

tenant_learned_rules (migration 028) post-dates the RLS_TABLES loop in 001/021,
so it never received a row-level-security policy — tenant isolation rested
ENTIRELY on the service-layer ``WHERE tenant_id =`` clauses. This adds the same
defense-in-depth net every other tenant table has, using the
``get_current_tenant_id()`` STABLE function introduced in 021.

Chains off 079 (the single live head); do NOT touch the historical 027 head.
"""

from alembic import op

# revision identifiers
revision = "080_learned_rules_rls"
down_revision = "079_order_ref_pattern"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenant_learned_rules ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_learned_rules_tenant_isolation ON tenant_learned_rules
        USING (tenant_id = get_current_tenant_id())
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_learned_rules_tenant_isolation ON tenant_learned_rules")
    op.execute("ALTER TABLE tenant_learned_rules DISABLE ROW LEVEL SECURITY")
