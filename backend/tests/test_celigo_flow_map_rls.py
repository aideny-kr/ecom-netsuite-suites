# backend/tests/test_celigo_flow_map_rls.py
"""Migration 094 (`celigo_flow_map`) — seven flow-map tables, all ENABLE + FORCE
row-level security. Mirrors `test_user_dashboard_preference_model.py`'s
catalog-check pattern (092) plus `test_metric_rls_policy.py`'s genuine
non-bypass-role pattern (082), combined into ONE test that actually proves
FORCE matters -- not just that the flag is set.

WHY THE "GENUINE" TEST USES TABLE OWNERSHIP, NOT A ROLE WITH THE BYPASSRLS
ATTRIBUTE -- read this before changing the role-switching logic below.

The task brief asked for a test that would fail if a role "with BYPASSRLS"
could read cross-tenant. That phrasing does not survive contact with real
PostgreSQL semantics, verified empirically against this repo's own scratch DB
(`celigo_mig_test`) before writing this file:

    * A role with the actual BYPASSRLS attribute (or a superuser) bypasses
      row security UNCONDITIONALLY -- FORCE ROW LEVEL SECURITY has NO effect
      on it, full stop. Confirmed live: `rls_probe` owned by the connecting
      `postgres` role (rolsuper=t, rolbypassrls=t) returned BOTH tenants'
      rows before AND after `ALTER TABLE ... FORCE ROW LEVEL SECURITY`.
      Writing a test asserting "a BYPASSRLS role can't cross-tenant read"
      would assert something PostgreSQL cannot do -- it would encode a
      falsehood, the exact failure class `observed-shapes.md` (Task 3) spent
      itself warning about.
    * FORCE affects exactly one thing: the TABLE OWNER, when that owner is
      NOT itself a superuser/BYPASSRLS role. Without FORCE, an owner bypasses
      RLS by ownership (a *different* mechanism than BYPASSRLS). WITH FORCE,
      the owner is subject to the policy like anyone else. Confirmed live,
      same scratch DB: a fresh `NOLOGIN NOSUPERUSER NOBYPASSRLS` role made
      owner of `rls_probe` saw both tenants' rows with `NO FORCE` and only
      its own tenant's row with `FORCE` set -- same table, same policy, only
      the FORCE flag changed.

This is exactly the case the 092/084 migration comments already name:
"FORCE is load-bearing on Supabase (table owner is not BYPASSRLS)" -- i.e.
Supabase's table-owning role is an owner-without-bypass, which is precisely
the role class this test exercises. `test_all_tables_force_rls_blocks_cross_tenant_owner_read`
below is RED on a migration that only does `ENABLE ROW LEVEL SECURITY`
(confirmed by temporarily commenting out the `FORCE ROW LEVEL SECURITY`
statements in the migration and re-running -- see task-4-report.md) and GREEN
once FORCE is restored.

Skips (both tests) when the connecting role lacks CREATEROLE, leaving the
catalog-presence pin in migration review as the durable, always-checkable
signal -- same posture as `test_metric_rls_policy.py`.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy.exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import create_test_tenant

# Table creation/dependency order (parents before children) -- also the order
# migration 094 creates them in.
_TABLES = (
    "celigo_integrations",
    "celigo_flows",
    "celigo_flow_steps",
    "celigo_scripts",
    "celigo_script_attachments",
    "celigo_error_signatures",
    "celigo_flow_errors",
)


async def _make_connection(db: AsyncSession, tenant_id) -> uuid.UUID:
    """Raw SQL, not the ``Connection`` ORM model: ``app.services.celigo_write_guard``
    refuses any ORM flush of a ``provider='celigo'`` row outside the paired
    connect/disconnect endpoints (see its module docstring, point 1 -- raw SQL
    below the ORM is the documented, accepted way for tests/scripts to seed a
    row without routing through that guard). This test needs a real
    ``connections`` row to satisfy the flow-map tables' FK, nothing about the
    write-guard's own behavior."""
    conn_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO connections (id, tenant_id, provider, label, status, encrypted_credentials, encryption_key_version) "
            "VALUES (:id, :tenant_id, 'celigo', 'Celigo', 'active', 'unit-test-not-a-real-token', 1)"
        ).bindparams(id=conn_id, tenant_id=tenant_id)
    )
    await db.flush()
    return conn_id


async def _seed_chain(db: AsyncSession, tenant_id, connection_id) -> dict[str, uuid.UUID]:
    """Insert one row into EACH of the seven tables for *tenant_id*, chained
    integration -> flow -> flow_step -> (script + attachment), plus an
    independent error_signature -> flow_error. Returns each row's id keyed by
    table name so a caller can assert on exactly which rows are/aren't
    visible under a given role/tenant context.

    Raw SQL (not the ORM) on purpose: Task 5's models don't exist yet -- this
    test only depends on migration 094's table shape, nothing else."""
    ids: dict[str, uuid.UUID] = {}
    suffix = uuid.uuid4().hex[:8]

    integration_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO celigo_integrations (id, tenant_id, celigo_connection_id, celigo_id, name, raw_json) "
            "VALUES (:id, :tenant_id, :conn_id, :celigo_id, 'Test Integration', '{}'::jsonb)"
        ).bindparams(id=integration_id, tenant_id=tenant_id, conn_id=connection_id, celigo_id=f"int_{suffix}")
    )
    ids["celigo_integrations"] = integration_id

    flow_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO celigo_flows (id, tenant_id, celigo_connection_id, integration_id, celigo_id, name, raw_json) "
            "VALUES (:id, :tenant_id, :conn_id, :integration_id, :celigo_id, 'Test Flow', '{}'::jsonb)"
        ).bindparams(
            id=flow_id,
            tenant_id=tenant_id,
            conn_id=connection_id,
            integration_id=integration_id,
            celigo_id=f"flow_{suffix}",
        )
    )
    ids["celigo_flows"] = flow_id

    step_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO celigo_flow_steps (id, tenant_id, celigo_connection_id, flow_id, celigo_id, role, raw_json) "
            "VALUES (:id, :tenant_id, :conn_id, :flow_id, :celigo_id, 'processor', '{}'::jsonb)"
        ).bindparams(id=step_id, tenant_id=tenant_id, conn_id=connection_id, flow_id=flow_id, celigo_id=f"exp_{suffix}")
    )
    ids["celigo_flow_steps"] = step_id

    script_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO celigo_scripts (id, tenant_id, celigo_connection_id, celigo_id, name) "
            "VALUES (:id, :tenant_id, :conn_id, :celigo_id, 'Test Script')"
        ).bindparams(id=script_id, tenant_id=tenant_id, conn_id=connection_id, celigo_id=f"scr_{suffix}")
    )
    ids["celigo_scripts"] = script_id

    attachment_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO celigo_script_attachments "
            "(id, tenant_id, celigo_connection_id, flow_id, flow_step_id, script_id, script_celigo_id, json_path) "
            "VALUES (:id, :tenant_id, :conn_id, :flow_id, :step_id, :script_id, :script_celigo_id, :json_path)"
        ).bindparams(
            id=attachment_id,
            tenant_id=tenant_id,
            conn_id=connection_id,
            flow_id=flow_id,
            step_id=step_id,
            script_id=script_id,
            script_celigo_id=f"scr_{suffix}",
            json_path=f"pageProcessors[0].transform.script.{suffix}",
        )
    )
    ids["celigo_script_attachments"] = attachment_id

    signature_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO celigo_error_signatures (id, tenant_id, celigo_connection_id, fingerprint) "
            "VALUES (:id, :tenant_id, :conn_id, :fingerprint)"
        ).bindparams(id=signature_id, tenant_id=tenant_id, conn_id=connection_id, fingerprint=f"sig_{suffix}")
    )
    ids["celigo_error_signatures"] = signature_id

    error_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO celigo_flow_errors "
            "(id, tenant_id, celigo_connection_id, flow_id, flow_step_id, signature_id, celigo_id) "
            "VALUES (:id, :tenant_id, :conn_id, :flow_id, :step_id, :signature_id, :celigo_id)"
        ).bindparams(
            id=error_id,
            tenant_id=tenant_id,
            conn_id=connection_id,
            flow_id=flow_id,
            step_id=step_id,
            signature_id=signature_id,
            celigo_id=f"err_{suffix}",
        )
    )
    ids["celigo_flow_errors"] = error_id

    await db.flush()
    return ids


@pytest.mark.parametrize("table_name", _TABLES)
async def test_all_tables_have_rls_enabled_and_forced(db: AsyncSession, table_name: str):
    """Catalog pin -- works under ANY role (pure pg_class read), so this stays
    the durable, always-checkable gate even in an environment where the
    genuine behavioral test below has to skip for lack of CREATEROLE."""
    row = (
        await db.execute(
            text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t").bindparams(t=table_name)
        )
    ).first()
    assert row is not None, f"{table_name}: table does not exist"
    rls_enabled, rls_forced = row
    assert rls_enabled, f"{table_name}: ROW LEVEL SECURITY is not enabled"
    assert rls_forced, f"{table_name}: FORCE ROW LEVEL SECURITY is not set"

    pol = (
        await db.execute(
            text(
                "SELECT pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid) "
                "FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid WHERE c.relname = :t"
            ).bindparams(t=table_name)
        )
    ).first()
    assert pol is not None, f"{table_name}: no RLS policy defined"
    using, with_check = pol
    assert using and "get_current_tenant_id()" in using, f"{table_name}: USING clause missing get_current_tenant_id()"
    assert with_check and "get_current_tenant_id()" in with_check, (
        f"{table_name}: WITH CHECK clause missing get_current_tenant_id()"
    )


@pytest.mark.parametrize("table_name", _TABLES)
async def test_all_tables_force_rls_blocks_cross_tenant_owner_read(db: AsyncSession, table_name: str):
    """THE test that matters (see module docstring for why this is
    ownership-based, not a literal BYPASSRLS-attribute role).

    Seeds one full seven-table chain for tenant A and one for tenant B, makes
    a fresh non-superuser/non-bypass role the OWNER of *table_name*, then
    reads it as that role under tenant B's context. Tenant A's row must be
    invisible and tenant B's row must be the only one returned.

    RED without FORCE: an owner-without-bypass bypasses RLS by ownership
    alone, so this SELECT returns both tenants' rows on a migration that only
    does ENABLE ROW LEVEL SECURITY. Confirmed by temporarily dropping the
    `FORCE ROW LEVEL SECURITY` statements from the migration and re-running
    this exact test (see task-4-report.md for the red output)."""
    tenant_a = await create_test_tenant(db, name=f"Tenant A {uuid.uuid4().hex[:6]}")
    tenant_b = await create_test_tenant(db, name=f"Tenant B {uuid.uuid4().hex[:6]}")
    conn_a = await _make_connection(db, tenant_a.id)
    conn_b = await _make_connection(db, tenant_b.id)
    ids_a = await _seed_chain(db, tenant_a.id, conn_a)
    ids_b = await _seed_chain(db, tenant_b.id, conn_b)
    await db.flush()

    role = f"_celigo_rls_probe_{uuid.uuid4().hex[:12]}"
    try:
        await db.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS'))
    except sqlalchemy.exc.ProgrammingError:
        pytest.skip("connecting role lacks CREATEROLE -- catalog-presence test is the durable gate")

    original_owner = (
        await db.execute(text("SELECT tableowner FROM pg_tables WHERE tablename = :t").bindparams(t=table_name))
    ).scalar_one()

    try:
        # Execute privilege on the tenant-context function is ordinarily
        # PUBLIC-granted (no REVOKE exists in this schema's migration
        # history), but grant explicitly anyway -- defensive, matches
        # test_metric_rls_policy.py's established pattern, and costs nothing
        # if it's already implied.
        await db.execute(text(f'GRANT EXECUTE ON FUNCTION get_current_tenant_id() TO "{role}"'))
        # Table ownership is the ONLY thing FORCE affects (see module
        # docstring) -- a plain GRANTed non-owner role is already subject to
        # RLS the moment ENABLE is set, with or without FORCE, so it could
        # never distinguish the two.
        await db.execute(text(f'ALTER TABLE {table_name} OWNER TO "{role}"'))

        await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_b.id}'"))
        await db.execute(text(f'SET LOCAL ROLE "{role}"'))

        rows = (await db.execute(text(f"SELECT tenant_id FROM {table_name}"))).all()
        seen_tenants = {r[0] for r in rows}

        assert tenant_a.id not in seen_tenants, (
            f"{table_name}: tenant B (owner role, tenant-B context) could read tenant A's row "
            f"({ids_a[table_name]}) -- FORCE ROW LEVEL SECURITY is not applying to the table owner."
        )
        assert seen_tenants == {tenant_b.id}, (
            f"{table_name}: expected only tenant B's own row ({ids_b[table_name]}) visible under "
            f"RLS, got tenant ids {seen_tenants}"
        )
    finally:
        # RESET ROLE before any ownership/role cleanup: as the probe role
        # (which now OWNS the table), reassigning ownership back is subject
        # to membership rules that don't apply once we're back to the
        # superuser session role.
        await db.execute(text("RESET ROLE"))
        await db.execute(text(f'ALTER TABLE {table_name} OWNER TO "{original_owner}"'))
        # DROP OWNED BY revokes every grant/ownership the role picked up so
        # DROP ROLE doesn't fail with DependentObjectsStillExistError. All of
        # this rolls back with the fixture's outer transaction regardless.
        await db.execute(text(f'DROP OWNED BY "{role}"'))
        await db.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await db.flush()
