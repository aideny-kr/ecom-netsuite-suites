"""metric_definitions RLS WITH CHECK — pin every write to the caller's own tenant context

Root cause this closes: migration 080's policy is USING-only:

    USING (tenant_id = get_current_tenant_id() OR tenant_id = SYSTEM::uuid)

PostgreSQL uses the USING expression as the IMPLICIT WITH CHECK for INSERT/UPDATE when
no explicit WITH CHECK exists. Because that USING carries the `OR tenant_id = SYSTEM`
read branch, the implicit write-check passes for ANY tenant-context session writing a
tenant_id=SYSTEM row — so the DB was NOT a write backstop (a tenant could persist a
SYSTEM-default row). 081's FORCE RLS is what first subjects the table-owning app role to
this on Supabase (the owner there is NOT BYPASSRLS). Empirically proven under a genuine
non-bypass role: a tenant-context INSERT of a SYSTEM row SUCCEEDED on head 081.

The repo idiom is explicit elsewhere: 021_rls_stable_function.py:96-97 uses
`FOR INSERT WITH CHECK (tenant_id = get_current_tenant_id())` for audit_events. This
migration brings metric_definitions to that parity.

DROP + CREATE (not ALTER POLICY) because ALTER POLICY cannot add a WITH CHECK where none
existed. The USING clause is kept BYTE-IDENTICAL to 080:64-67 so reads of tenant ∪ SYSTEM
are unchanged (seeded SYSTEM defaults stay visible to every tenant, and
test_metric_definitions_rls_policy_exists_with_system_clause's OR-SYSTEM assertion still
passes). The new WITH CHECK pins every INSERT/UPDATE to the caller's OWN active tenant
context — no OR-SYSTEM — so the legit SYSTEM write must run under SYSTEM context (the
seeder already does; create_metric/update_metric now do too for SYSTEM_TENANT_ID).
"""

from alembic import op

revision = "082_metric_def_with_check"
down_revision = "081_metric_definitions_hardening"
branch_labels = None
depends_on = None

SYSTEM_TENANT = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    op.execute("DROP POLICY IF EXISTS metric_definitions_tenant_isolation ON metric_definitions")
    # USING is byte-identical to 080:64-67 (reads of tenant ∪ SYSTEM unchanged).
    # WITH CHECK pins writes to the caller's own active tenant context (no OR-SYSTEM):
    # closes the tenant→SYSTEM write hole AND, as a defense-in-depth bonus, blocks a
    # tenant-context write of ANOTHER tenant's row.
    op.execute(f"""
        CREATE POLICY metric_definitions_tenant_isolation ON metric_definitions
        USING (tenant_id = get_current_tenant_id() OR tenant_id = '{SYSTEM_TENANT}'::uuid)
        WITH CHECK (tenant_id = get_current_tenant_id())
    """)


def downgrade() -> None:
    # Restore the original 080 USING-only policy verbatim (NO WITH CHECK).
    op.execute("DROP POLICY IF EXISTS metric_definitions_tenant_isolation ON metric_definitions")
    op.execute(f"""
        CREATE POLICY metric_definitions_tenant_isolation ON metric_definitions
        USING (tenant_id = get_current_tenant_id() OR tenant_id = '{SYSTEM_TENANT}'::uuid)
    """)
