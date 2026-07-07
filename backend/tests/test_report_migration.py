# backend/tests/test_report_migration.py
import uuid

import pytest
import sqlalchemy.exc
from sqlalchemy import text

from app.core.database import set_tenant_context
from app.models.report import Report
from tests.conftest import create_test_tenant  # pattern: test_saved_queries.py


async def test_reports_table_columns_exist(db):
    cols = (
        (await db.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='reports'")))
        .scalars()
        .all()
    )
    assert {
        "id",
        "tenant_id",
        "title",
        "spec_json",
        "rendered_html",
        "status",
        "source_run_id",
        "created_by",
        "version",
        "published_drive_url",
        "published_at",
        "created_at",
        "updated_at",
    } <= set(cols)


async def test_reports_recipe_json_column_exists_nullable_jsonb(db):
    """Slice A (live-dashboard reports): the captured refresh recipe. Nullable — historic
    reports legitimately lack a recipe and stay snapshot-only (spec §4A: no backfill)."""
    row = (
        await db.execute(
            text(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name='reports' AND column_name='recipe_json'"
            )
        )
    ).first()
    assert row is not None, "reports.recipe_json missing — migration 086 not applied"
    data_type, is_nullable = row
    assert data_type == "jsonb"
    assert is_nullable == "YES"


async def test_reports_rls_is_forced_with_tenant_policy(db):
    """Pin the migration's intent at the catalog level (always valid, even under the
    local BYPASSRLS `postgres` role): RLS is ENABLED + FORCE'd and the policy pins BOTH
    USING and WITH CHECK to get_current_tenant_id() with NO OR-SYSTEM branch (reports are
    never SYSTEM-owned). Mirrors test_metric_rls_policy.py's catalog-presence gate."""
    rls = (
        await db.execute(text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='reports'"))
    ).first()
    assert rls is not None and rls[0] and rls[1], "reports must have RLS ENABLED + FORCE'd"

    pol = (
        await db.execute(
            text(
                "SELECT pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid) "
                "FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid WHERE c.relname='reports'"
            )
        )
    ).first()
    assert pol is not None, "reports has no RLS policy"
    using, with_check = pol
    assert using and "get_current_tenant_id()" in using
    assert with_check and "get_current_tenant_id()" in with_check, "policy must pin writes to the caller's tenant"
    # reports are never SYSTEM-owned — the OR-SYSTEM read clause must NOT appear.
    assert "00000000-0000-0000-0000-000000000000" not in (using + with_check)


async def test_reports_rls_blocks_cross_tenant(db):
    """Genuine policy test: a report written as tenant A is invisible under tenant B's
    context. The local `db` fixture runs as the BYPASSRLS `postgres` owner, so FORCE RLS
    is suppressed for it — we therefore SET LOCAL ROLE to a fresh NOLOGIN non-bypass role
    (repo idiom from test_metric_rls_policy.py) to genuinely subject the SELECT to the
    policy.

    Skips cleanly when the environment cannot create/enter a non-bypass role — on the
    Supabase test DB the `postgres` user lacks CREATEROLE and cannot SET ROLE to a fresh
    role (GRANT ... TO CURRENT_USER even severs the managed session). In that case the
    catalog-presence test above + the Task 15 live smoke against `uat-smoke` are the
    authoritative policy gates; this test then just guards the ORM/model round-trip."""
    tenant_a = await create_test_tenant(db, name="Corp A")
    tenant_b = await create_test_tenant(db, name="Corp B")

    # Seed the row AS THE OWNER (before SET ROLE) so its presence is unconditional.
    await set_tenant_context(db, str(tenant_a.id))
    db.add(
        Report(
            tenant_id=tenant_a.id,
            title="A report",
            spec_json={"sections": []},
            rendered_html="<html></html>",
            created_by=None,
        )
    )
    await db.flush()

    # All probe-role DDL/grants + the SET ROLE read run inside ONE nested savepoint that is
    # ALWAYS rolled back (we raise _CapturedError to abort it) — so the throwaway role + its
    # grants never linger as dependent objects (which would block DROP ROLE on a managed
    # Supabase DB) and the outer fixture transaction stays intact. ANY privilege failure
    # (no CREATEROLE; cannot SET ROLE to a non-member role) also rolls the savepoint back,
    # and we skip: the catalog test + the Task 15 live smoke are then the policy gates.
    await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_b.id}'"))
    conn = await db.connection()
    role = f"_rls_probe_{uuid.uuid4().hex[:12]}"

    class _CapturedError(Exception):
        rows: list

    try:
        async with conn.begin_nested():
            await conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
            await conn.execute(text(f'GRANT SELECT ON reports TO "{role}"'))
            await conn.execute(text(f'GRANT EXECUTE ON FUNCTION get_current_tenant_id() TO "{role}"'))
            await conn.execute(text(f'SET LOCAL ROLE "{role}"'))
            # Under tenant B's context + the non-bypass role, RLS must hide tenant A's row.
            captured = (await conn.execute(text("SELECT id FROM reports"))).all()  # NO .where
            await conn.execute(text("RESET ROLE"))
            exc = _CapturedError()
            exc.rows = captured
            raise exc  # abort the savepoint → discards the role + grants cleanly
    except _CapturedError as done:
        rows = done.rows
    except (sqlalchemy.exc.ProgrammingError, sqlalchemy.exc.DBAPIError):
        pytest.skip(
            "cannot create/enter a non-bypass role here (managed Supabase) — the catalog "
            "test + the Task 15 live smoke are the authoritative policy gates"
        )
    assert rows == [], "FORCE RLS must hide tenant A's report from tenant B's context"
