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


async def test_has_recipe_flag_reflects_recipe_presence(client, db):
    """Slice A: the API exposes ONLY a has_recipe boolean (never the raw recipe —
    params embed full SQL); the FE shows Refresh iff a recipe exists (Slice B)."""
    ta = await create_test_tenant(db, name="A3")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    plain = Report(
        tenant_id=ta.id, title="Snapshot", spec_json={"sections": []}, rendered_html="<html></html>", created_by=ua.id
    )
    with_recipe = Report(
        tenant_id=ta.id,
        title="Live",
        spec_json={"sections": []},
        rendered_html="<html></html>",
        created_by=ua.id,
        recipe_json={"schema_version": 1, "captured_at": "t", "sections": [], "sources": {}},
    )
    db.add(plain)
    db.add(with_recipe)
    await db.flush()

    headers = make_auth_headers(ua)
    body_plain = (await client.get(f"/api/v1/reports/{plain.id}", headers=headers)).json()
    body_live = (await client.get(f"/api/v1/reports/{with_recipe.id}", headers=headers)).json()
    assert body_plain["has_recipe"] is False
    assert body_live["has_recipe"] is True
    assert "recipe_json" not in body_plain and "recipe_json" not in body_live  # never the raw recipe


# --- Slice B: refresh + versions endpoints --------------------------------------------


def _recipe_v1():
    return {
        "schema_version": 1,
        "captured_at": "2026-07-06T18:00:00+00:00",
        "sections": [{"type": "table", "result_id": "r1", "label": "T"}],
        "sources": {"r1": {"tool": "netsuite_suiteql", "params": {"query": "SELECT 1"}, "connection_id": None}},
    }


def _patch_refresh_executor(monkeypatch, amount=777):
    import json as _json

    async def fake_execute(tool_name, tool_input, tenant_id, actor_id, correlation_id, db, **kw):
        return _json.dumps(
            {
                "success": True,
                "columns": ["account", "amount"],
                "rows": [["Cash", amount]],
                "row_count": 1,
                "query": "SELECT 1",
            }
        )

    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", fake_execute)


_DEFAULT_RECIPE = object()  # sentinel: distinguish "use the default recipe" from "no recipe"


async def _seed_recipe_report(db, *, recipe=_DEFAULT_RECIPE, html="<html>original</html>"):
    ta = await create_test_tenant(db, name="RefreshAPI")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r = Report(
        tenant_id=ta.id,
        title="Live",
        spec_json={"sections": []},
        rendered_html=html,
        created_by=ua.id,
        recipe_json=_recipe_v1() if recipe is _DEFAULT_RECIPE else recipe,
    )
    db.add(r)
    await db.flush()
    return ta, ua, r


async def test_refresh_endpoint_publishes_and_serves_new_version(client, db, monkeypatch):
    ta, ua, r = await _seed_recipe_report(db)
    _patch_refresh_executor(monkeypatch, amount=777)
    headers = make_auth_headers(ua)

    res = await client.post(f"/api/v1/reports/{r.id}/refresh", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["version"] == 2
    assert body["has_recipe"] is True
    assert body["last_refreshed_at"] is not None

    view = await client.get(f"/api/v1/reports/{r.id}/view", headers=headers)
    assert "777.00" in view.text  # the stable URL now serves the refreshed numbers
    assert 'class="stamp"' in view.text


async def test_refresh_snapshot_only_409_and_unauth_401(client, db, monkeypatch):
    ta, ua, r = await _seed_recipe_report(db, recipe=None)
    _patch_refresh_executor(monkeypatch)
    headers = make_auth_headers(ua)
    assert (await client.post(f"/api/v1/reports/{r.id}/refresh", headers=headers)).status_code == 409
    assert (await client.post(f"/api/v1/reports/{r.id}/refresh")).status_code == 401
    assert (await client.post("/api/v1/reports/not-a-uuid/refresh", headers=headers)).status_code == 404


async def test_refresh_debounce_surfaces_429_with_retry_after(client, db, monkeypatch):
    ta, ua, r = await _seed_recipe_report(db)
    _patch_refresh_executor(monkeypatch)
    headers = make_auth_headers(ua)
    assert (await client.post(f"/api/v1/reports/{r.id}/refresh", headers=headers)).status_code == 200
    second = await client.post(f"/api/v1/reports/{r.id}/refresh", headers=headers)
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) > 0


async def test_versions_list_and_historical_view(client, db, monkeypatch):
    ta, ua, r = await _seed_recipe_report(db, html="<html>original</html>")
    headers = make_auth_headers(ua)

    # pre-refresh: a single synthesized entry derived from the parent
    listed = (await client.get(f"/api/v1/reports/{r.id}/versions", headers=headers)).json()
    assert len(listed) == 1
    assert listed[0]["version"] == 1 and listed[0]["is_current"] is True

    _patch_refresh_executor(monkeypatch)
    assert (await client.post(f"/api/v1/reports/{r.id}/refresh", headers=headers)).status_code == 200

    listed = (await client.get(f"/api/v1/reports/{r.id}/versions", headers=headers)).json()
    assert [v["version"] for v in listed] == [2, 1]  # desc
    assert listed[0]["is_current"] is True and listed[1]["is_current"] is False

    v1 = await client.get(f"/api/v1/reports/{r.id}/versions/1/view", headers=headers)
    assert v1.status_code == 200 and v1.text == "<html>original</html>"
    assert (await client.get(f"/api/v1/reports/{r.id}/versions/99/view", headers=headers)).status_code == 404


# --- Slice C: auto-refresh settings + resume (same get_current_user+RLS gate as all
# report routes — see the §6.3 permission note above the refresh endpoint) -------------


async def test_report_response_exposes_auto_refresh_ladder_state(client, db):
    """The FE derives the selector + staleness/paused banners from these three fields."""
    ta, ua, r = await _seed_recipe_report(db)
    body = (await client.get(f"/api/v1/reports/{r.id}", headers=make_auth_headers(ua))).json()
    assert body["auto_refresh"] == "daily"  # §6.1 default
    assert body["refresh_failure_count"] == 0
    assert body["auto_refresh_paused_at"] is None


async def test_patch_settings_roundtrip_each_interval_and_audits(client, db):
    ta, ua, r = await _seed_recipe_report(db)
    headers = make_auth_headers(ua)
    for value in ("off", "hourly", "daily"):
        res = await client.patch(f"/api/v1/reports/{r.id}/settings", headers=headers, json={"auto_refresh": value})
        assert res.status_code == 200, res.text
        assert res.json()["auto_refresh"] == value
    audit = (
        await db.execute(
            text(
                "SELECT count(*), min(actor_type) FROM audit_events "
                "WHERE action='report.settings_update' AND resource_id=:rid AND actor_id=:aid"
            ),
            {"rid": str(r.id), "aid": str(ua.id)},
        )
    ).first()
    assert audit[0] == 3 and audit[1] == "user"


async def test_patch_settings_rejects_unknown_interval_422(client, db):
    ta, ua, r = await _seed_recipe_report(db)
    res = await client.patch(
        f"/api/v1/reports/{r.id}/settings", headers=make_auth_headers(ua), json={"auto_refresh": "weekly"}
    )
    assert res.status_code == 422


async def test_patch_settings_snapshot_only_409_for_scheduling(client, db):
    """Legacy/snapshot reports stay snapshot-only (§6.1): scheduling them is a 409;
    'off' is always accepted (inert either way)."""
    ta, ua, r = await _seed_recipe_report(db, recipe=None)
    headers = make_auth_headers(ua)
    assert (
        await client.patch(f"/api/v1/reports/{r.id}/settings", headers=headers, json={"auto_refresh": "hourly"})
    ).status_code == 409
    assert (
        await client.patch(f"/api/v1/reports/{r.id}/settings", headers=headers, json={"auto_refresh": "off"})
    ).status_code == 200


async def test_patch_settings_unauth_401_and_malformed_404(client, db):
    """Cross-tenant invisibility is NOT client-testable here (the fixture session is
    the BYPASSRLS postgres owner — see test_view_cross_tenant_is_rls_invisible, which
    proves the policy through a non-bypass role; settings rides the same _get_owned
    None→404 path as every report route)."""
    ta, ua, r = await _seed_recipe_report(db)
    assert (await client.patch(f"/api/v1/reports/{r.id}/settings", json={"auto_refresh": "off"})).status_code == 401
    assert (
        await client.patch(
            "/api/v1/reports/not-a-uuid/settings", headers=make_auth_headers(ua), json={"auto_refresh": "off"}
        )
    ).status_code == 404


async def test_resume_clears_pause_resets_count_and_audits(client, db):
    """The one-click resume after reconnect (§4C): clears auto_refresh_paused_at AND
    zeroes the count (otherwise one stale failure re-pauses almost immediately).
    Idempotent — resuming a never-paused report is a 200 no-op."""
    from datetime import datetime, timezone

    ta, ua, r = await _seed_recipe_report(db)
    r.auto_refresh_paused_at = datetime.now(timezone.utc)
    r.refresh_failure_count = 7
    await db.flush()
    headers = make_auth_headers(ua)

    res = await client.post(f"/api/v1/reports/{r.id}/auto-refresh/resume", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["auto_refresh_paused_at"] is None
    assert body["refresh_failure_count"] == 0

    audit = (
        await db.execute(
            text(
                "SELECT actor_id, actor_type FROM audit_events "
                "WHERE action='report.auto_refresh_resumed' AND resource_id=:rid"
            ),
            {"rid": str(r.id)},
        )
    ).first()
    assert audit is not None and audit[0] == ua.id and audit[1] == "user"

    again = await client.post(f"/api/v1/reports/{r.id}/auto-refresh/resume", headers=headers)
    assert again.status_code == 200  # idempotent
    assert (await client.post(f"/api/v1/reports/{r.id}/auto-refresh/resume")).status_code == 401
