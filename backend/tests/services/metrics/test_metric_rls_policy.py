# backend/tests/services/metrics/test_metric_rls_policy.py
"""DB-backed invariant: the metric_definitions RLS policy EXISTS with the OR-SYSTEM clause.

Why this test (and what it deliberately does NOT test):

The catalog seeds SYSTEM-default rows (tenant_id = SYSTEM_TENANT_ID) that every
tenant must be able to read, while still hiding one tenant's *override* rows from
another tenant. The migration (080) therefore must NOT use the plain
``USING (tenant_id = get_current_tenant_id())`` policy — that would make every
seeded SYSTEM default invisible. It must use the doc_chunks-style policy
``USING (tenant_id = get_current_tenant_id() OR tenant_id = SYSTEM_TENANT_ID)``.

This test asserts the *policy definition* is present and references BOTH
``get_current_tenant_id()`` AND the SYSTEM uuid — i.e. it pins the migration's
intent so a future migration that downgrades the policy to the plain form (which
would silently hide every seeded default) fails CI.

As of migration 081 this table IS ``FORCE``'d (see ``test_metric_definitions_rls_is_forced``
below, which pins ``relforcerowsecurity``). Before 081 the owner role bypassed RLS, which
is why this policy-clause test does not itself open a tenant context to assert row
filtering — that path is covered by the application-level ``OR tenant_id == SYSTEM_TENANT_ID``
filter in ``metric_resolver.resolve_metrics``, exercised by ``test_metric_resolver.py``.
This test pins the DB-side defense-in-depth policy clause; the FORCE test pins that the
policy actually applies to the owner.
"""

from sqlalchemy import text

from app.models.metric_definition import SYSTEM_TENANT_ID


async def test_metric_definitions_rls_policy_exists_with_system_clause(db):
    rows = (
        await db.execute(text("SELECT policyname, qual FROM pg_policies WHERE tablename = 'metric_definitions'"))
    ).all()

    # A policy must exist at all (RLS enabled + at least one policy).
    assert rows, "metric_definitions has no RLS policy — SYSTEM defaults are unprotected/invisible"

    # The qual (USING clause) must reference BOTH the tenant function AND the SYSTEM uuid.
    # If a future migration reverts to the plain `tenant_id = get_current_tenant_id()`
    # policy, the SYSTEM-default rows become invisible to every tenant and this fails.
    quals = [(name, (qual or "")) for name, qual in rows]
    system_uuid = str(SYSTEM_TENANT_ID)

    matching = [name for name, qual in quals if "get_current_tenant_id()" in qual and system_uuid in qual]
    assert matching, (
        "No metric_definitions RLS policy references BOTH get_current_tenant_id() and the "
        f"SYSTEM uuid {system_uuid}. Policies found: {quals}. The doc_chunks-style "
        "OR-SYSTEM clause is required so seeded SYSTEM-default metrics stay visible to "
        "every tenant. A plain `tenant_id = get_current_tenant_id()` policy silently hides them."
    )

    # RLS must actually be ENABLED on the table (a policy with RLS off is dead).
    rls_enabled = (
        await db.execute(text("SELECT relrowsecurity FROM pg_class WHERE relname = 'metric_definitions'"))
    ).scalar_one()
    assert rls_enabled, "ROW LEVEL SECURITY is not enabled on metric_definitions"


async def test_metric_definitions_rls_is_forced(db):
    """Migration 081 asserts FORCE ROW LEVEL SECURITY so the policy applies to the
    table owner (superuser bypass is suppressed). This test pins that invariant:
    relforcerowsecurity must be TRUE in pg_class after 081 is applied.

    WHY: without FORCE RLS the policy is vacuous for the DB role that owns the
    table — the owner sees all rows regardless of tenant context. That breaks
    the defense-in-depth guarantee that a mis-configured SET LOCAL still leaks
    zero cross-tenant rows. FORCE RLS makes the policy apply even to the owner.
    """
    force_rls = (
        await db.execute(text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'metric_definitions'"))
    ).scalar_one()
    assert force_rls, (
        "FORCE ROW LEVEL SECURITY is not enabled on metric_definitions. "
        "Migration 081 must apply ALTER TABLE metric_definitions FORCE ROW LEVEL SECURITY."
    )
