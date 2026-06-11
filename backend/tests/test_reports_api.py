# backend/tests/test_reports_api.py
import uuid

import pytest
import sqlalchemy.exc
from sqlalchemy import text

from app.core.database import set_tenant_context
from app.models.report import Report
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers


async def test_view_returns_html(client, db):
    """Owner can GET /reports/{id}/view and gets the rendered HTML back as text/html."""
    ta = await create_test_tenant(db, name="A")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r = Report(
        tenant_id=ta.id,
        title="A",
        spec_json={"sections": []},
        rendered_html="<!DOCTYPE html><html><body>HELLO</body></html>",
        created_by=ua.id,
    )
    db.add(r)
    await db.flush()

    resp = await client.get(f"/api/v1/reports/{r.id}/view", headers=make_auth_headers(ua))
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "HELLO" in resp.text


async def test_get_and_list_return_owned_report(client, db):
    """GET /reports lists the owner's report and GET /reports/{id} returns it."""
    ta = await create_test_tenant(db, name="A2")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r = Report(
        tenant_id=ta.id,
        title="Quarterly",
        spec_json={"sections": []},
        rendered_html="<html></html>",
        created_by=ua.id,
    )
    db.add(r)
    await db.flush()

    headers = make_auth_headers(ua)

    got = await client.get(f"/api/v1/reports/{r.id}", headers=headers)
    assert got.status_code == 200
    body = got.json()
    assert body["id"] == str(r.id)
    assert body["title"] == "Quarterly"
    assert body["status"] == "draft"
    assert body["version"] == 1

    listed = await client.get("/api/v1/reports", headers=headers)
    assert listed.status_code == 200
    assert str(r.id) in {row["id"] for row in listed.json()}


async def test_malformed_id_returns_404(client, db):
    """A non-UUID report id is a clean 404, never a 500."""
    ta = await create_test_tenant(db, name="A3")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))

    resp = await client.get("/api/v1/reports/not-a-uuid", headers=make_auth_headers(ua))
    assert resp.status_code == 404


async def test_view_cross_tenant_is_rls_invisible(db):
    """The endpoint's cross-tenant 404 is driven entirely by RLS hiding the row
    (``_get_owned`` → ``scalar_one_or_none()`` None → 404, spec §11). Prove that
    invisibility genuinely: write a report as tenant A, then SELECT it under tenant
    B's context through a fresh NOLOGIN non-bypass role so the SELECT is actually
    subject to the FORCE'd policy (the ``db`` fixture connects as the BYPASSRLS
    ``postgres`` owner, which would otherwise see every row — see
    test_report_migration.py for the same idiom).

    Skips cleanly on a managed Supabase DB where ``postgres`` lacks CREATEROLE — in
    that case test_report_migration.py's catalog-presence gate + the live smoke are
    the authoritative policy gates and this test only guards the ORM round-trip."""
    tenant_a = await create_test_tenant(db, name="Cross A")
    tenant_b = await create_test_tenant(db, name="Cross B")

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

    # All probe-role DDL/grants + the SET ROLE read run inside ONE nested savepoint
    # that is ALWAYS rolled back (raise _CapturedError to abort it) so the throwaway
    # role + its grants never linger (which would block DROP ROLE on managed Supabase)
    # and the outer fixture transaction stays intact.
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
            "test + the live smoke are the authoritative policy gates"
        )
    assert rows == [], "FORCE RLS must hide tenant A's report from tenant B's context (→ endpoint 404)"
