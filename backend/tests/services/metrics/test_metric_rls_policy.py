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

import uuid

import pytest
import sqlalchemy.exc
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


# ── 082: WITH CHECK write-backstop ────────────────────────────────────────────
#
# Migration 080's policy is USING-only. PostgreSQL uses the USING expression as the
# implicit WITH CHECK for INSERT/UPDATE, and that USING carries an `OR tenant_id =
# SYSTEM` branch — so ANY tenant-context session could write a tenant_id=SYSTEM row
# (the DB was not a write backstop). EMPIRICALLY PROVEN under a genuine non-bypass
# role: a tenant-context INSERT of a SYSTEM row SUCCEEDED on head 081. The 021
# audit_events policy uses the explicit-WITH-CHECK idiom (FOR INSERT WITH CHECK
# (tenant_id = get_current_tenant_id())); 082 brings metric_definitions to parity.
#
# Local postgres is rolsuper+rolbypassrls, so FORCE/WITH CHECK do NOT reject for the
# default role — the catalog-presence test below is the primary always-valid proof.
# The genuine-rejection test SET LOCAL ROLEs to a fresh NOLOGIN non-bypass role.


async def test_metric_definitions_rls_policy_has_with_check(db):
    """082: the policy MUST carry a WITH CHECK expression pinning every INSERT/UPDATE to
    the caller's OWN active tenant context — `tenant_id = get_current_tenant_id()` — with
    NO OR-SYSTEM allowance (the OR-SYSTEM clause is read-only, in USING).

    Works under BYPASSRLS (this is a pure catalog read). RED on head 081, where
    polwithcheck IS NULL (the USING expr is the implicit write-check, which carries the
    OR-SYSTEM branch and so lets a tenant write a SYSTEM row)."""
    with_check = (
        await db.execute(
            text(
                "SELECT pg_get_expr(polwithcheck, polrelid) "
                "FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
                "WHERE c.relname = 'metric_definitions'"
            )
        )
    ).scalar_one_or_none()

    assert with_check is not None, (
        "metric_definitions RLS policy has NO WITH CHECK (polwithcheck IS NULL). "
        "PostgreSQL then uses the USING expr — which carries OR tenant_id=SYSTEM — as the "
        "implicit write-check, so a tenant-context session can write a tenant_id=SYSTEM "
        "row. Migration 082 must add WITH CHECK (tenant_id = get_current_tenant_id())."
    )
    assert "get_current_tenant_id()" in with_check, (
        f"WITH CHECK must pin writes to the caller's own tenant context, got: {with_check!r}"
    )
    # The WITH CHECK must NOT carry the SYSTEM uuid — that is the read-only OR-SYSTEM
    # clause (which belongs in USING). Its presence in WITH CHECK would re-open the
    # tenant→SYSTEM write hole 082 closes.
    assert str(SYSTEM_TENANT_ID) not in with_check, (
        f"WITH CHECK must NOT allow tenant_id=SYSTEM (that is the read-only USING clause); got: {with_check!r}"
    )


async def test_metric_definitions_with_check_rejects_system_write_from_tenant_ctx(db):
    """082 (genuine rejection): under a non-bypass role with a RANDOM tenant in
    app.current_tenant_id, INSERTing a tenant_id=SYSTEM row MUST be rejected by the
    WITH CHECK (the write backstop). Local postgres is BYPASSRLS, so this uses
    `SET LOCAL ROLE <fresh NOLOGIN non-bypass role>` to genuinely subject the write to
    the policy.

    The RLS rejection surfaces through SQLAlchemy as sqlalchemy.exc.ProgrammingError
    wrapping asyncpg.exceptions.InsufficientPrivilegeError ('new row violates row-level
    security policy') — NOT a bare asyncpg.InsufficientPrivilegeError. Distinguish that
    from an IntegrityError (FK) — the SYSTEM tenant parent is provisioned (as the owner,
    BEFORE SET ROLE) so the only thing that can reject is the WITH CHECK.

    RED on head 081: there the tenant-context INSERT of a SYSTEM row SUCCEEDS (no
    WITH CHECK), so pytest.raises does NOT fire and the test fails.

    Skips when the role lacks CREATEROLE (CI), leaving the catalog-presence test as the
    durable gate."""
    role = f"_rls_probe_{uuid.uuid4().hex[:12]}"
    random_tenant = str(uuid.uuid4())

    # CREATE ROLE feasibility — skip cleanly if the test role lacks CREATEROLE.
    try:
        await db.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
    except sqlalchemy.exc.ProgrammingError:
        pytest.skip("test role lacks CREATEROLE — catalog-presence test is the durable gate")

    try:
        await db.execute(text(f'GRANT INSERT, SELECT ON metric_definitions TO "{role}"'))
        await db.execute(text(f'GRANT EXECUTE ON FUNCTION get_current_tenant_id() TO "{role}"'))

        # Provision the SYSTEM tenant FK parent AS THE OWNER (before SET ROLE) so an FK
        # IntegrityError can never masquerade as the RLS rejection we are asserting.
        await db.execute(
            text(
                "INSERT INTO tenants (id, name, slug, plan, is_active) "
                "VALUES (CAST(:id AS uuid), 'System', 'system', 'free', true) "
                "ON CONFLICT (id) DO NOTHING"
            ).bindparams(id=str(SYSTEM_TENANT_ID))
        )
        await db.flush()

        await db.execute(text(f"SET LOCAL app.current_tenant_id = '{random_tenant}'"))
        await db.execute(text(f'SET LOCAL ROLE "{role}"'))

        # Savepoint on the session's bound connection so the expected failure does not
        # poison the outer fixture transaction.
        conn = await db.connection()
        with pytest.raises((sqlalchemy.exc.ProgrammingError, sqlalchemy.exc.DBAPIError)) as ei:
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "INSERT INTO metric_definitions "
                        "(tenant_id, key, display_name, definition, unit, source_kind, status, version) "
                        "VALUES (CAST(:tid AS uuid), :key, 'x', 'x', 'currency', 'suiteql', 'active', 1)"
                    ).bindparams(tid=str(SYSTEM_TENANT_ID), key=f"probe_{uuid.uuid4().hex[:8]}")
                )
        assert "row-level security" in str(ei.value).lower(), f"expected an RLS WITH CHECK rejection, got: {ei.value!r}"
    finally:
        await db.execute(text("RESET ROLE"))
        # DROP OWNED BY revokes every grant made to the role so DROP ROLE does not fail
        # with DependentObjectsStillExistError (privileges on metric_definitions +
        # get_current_tenant_id()). All of this is rolled back by the fixture anyway.
        await db.execute(text(f'DROP OWNED BY "{role}"'))
        await db.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await db.flush()


async def test_metric_definitions_with_check_allows_system_write_under_system_ctx(db):
    """082 (positive, FIX-3): the SAME non-bypass role CAN insert a tenant_id=SYSTEM row
    when app.current_tenant_id IS the SYSTEM uuid — i.e. the legit seeder / superadmin
    SYSTEM-context path passes the WITH CHECK. Proves 082 blocks the hole without
    breaking the intended SYSTEM write.

    Skips when the role lacks CREATEROLE."""
    role = f"_rls_probe_{uuid.uuid4().hex[:12]}"

    try:
        await db.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
    except sqlalchemy.exc.ProgrammingError:
        pytest.skip("test role lacks CREATEROLE — catalog-presence test is the durable gate")

    try:
        await db.execute(text(f'GRANT INSERT, SELECT ON metric_definitions TO "{role}"'))
        await db.execute(text(f'GRANT EXECUTE ON FUNCTION get_current_tenant_id() TO "{role}"'))
        await db.execute(
            text(
                "INSERT INTO tenants (id, name, slug, plan, is_active) "
                "VALUES (CAST(:id AS uuid), 'System', 'system', 'free', true) "
                "ON CONFLICT (id) DO NOTHING"
            ).bindparams(id=str(SYSTEM_TENANT_ID))
        )
        await db.flush()

        # SYSTEM context — get_current_tenant_id() == SYSTEM == the row tenant_id.
        await db.execute(text(f"SET LOCAL app.current_tenant_id = '{SYSTEM_TENANT_ID}'"))
        await db.execute(text(f'SET LOCAL ROLE "{role}"'))

        conn = await db.connection()
        probe_key = f"probe_ok_{uuid.uuid4().hex[:8]}"
        async with conn.begin_nested():
            await conn.execute(
                text(
                    "INSERT INTO metric_definitions "
                    "(tenant_id, key, display_name, definition, unit, source_kind, status, version) "
                    "VALUES (CAST(:tid AS uuid), :key, 'x', 'x', 'currency', 'suiteql', 'active', 1)"
                ).bindparams(tid=str(SYSTEM_TENANT_ID), key=probe_key)
            )
            # The row landed (visible under SYSTEM context).
            found = (
                await conn.execute(text("SELECT 1 FROM metric_definitions WHERE key = :key").bindparams(key=probe_key))
            ).scalar_one_or_none()
            assert found == 1, "SYSTEM-context write under the non-bypass role should pass WITH CHECK"
    finally:
        await db.execute(text("RESET ROLE"))
        # DROP OWNED BY revokes every grant made to the role so DROP ROLE does not fail
        # with DependentObjectsStillExistError. Rolled back by the fixture anyway.
        await db.execute(text(f'DROP OWNED BY "{role}"'))
        await db.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await db.flush()
