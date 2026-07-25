# backend/tests/test_dashboard_api.py
"""GET /dashboard + PUT/DELETE /dashboard/active — the per-user wallpaper API.

Pattern mirrors test_reports_api.py's pin/unpin section: `_get_owned`-style
404 parity, flush/audit-then-commit ordering, `_to_response` UUID
stringification. `_get_visible_report` here is deliberately NOT ownership
gated (unlike reports.py's `_get_owned` + `_can_manage`) — any tenant member
may choose any published report as their own wallpaper."""

import uuid

from sqlalchemy import select, text

from app.core.database import set_tenant_context
from app.models.report import Report
from app.models.user_dashboard_preference import UserDashboardPreference
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers


async def _seed_report(db, ta, ua, *, title: str = "R"):
    r = Report(
        tenant_id=ta.id,
        title=title,
        spec_json={"sections": []},
        rendered_html=f"<html>{title}</html>",
        created_by=ua.id,
    )
    db.add(r)
    await db.flush()
    return r


async def _pin(db, r):
    """Publish a report with a real, comparable timestamp (not a SQL
    expression) so tests can compare orderings across multiple pins."""
    from datetime import datetime, timezone

    r.dashboard_pinned_at = datetime.now(timezone.utc)
    await db.flush()
    return r


# --- GET /dashboard ---------------------------------------------------------


async def test_get_empty_tenant_returns_empty_published_and_null_active(client, db):
    ta = await create_test_tenant(db, name="EmptyDash")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))

    resp = await client.get("/api/v1/dashboard", headers=make_auth_headers(ua))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["published"] == []
    assert body["active"] is None
    assert body["active_is_fallback"] is False


async def test_get_never_chosen_user_falls_back_to_most_recent_published(client, db):
    ta = await create_test_tenant(db, name="NeverChosen")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="Older")
    await _pin(db, r1)
    r2 = await _seed_report(db, ta, ua, title="Newer")
    await _pin(db, r2)

    resp = await client.get("/api/v1/dashboard", headers=make_auth_headers(ua))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {row["id"] for row in body["published"]} == {str(r1.id), str(r2.id)}
    # newest-published first
    assert body["published"][0]["id"] == str(r2.id)
    assert body["active"]["id"] == str(r2.id)
    assert body["active_is_fallback"] is False


async def test_get_chosen_user_sees_their_choice(client, db):
    ta = await create_test_tenant(db, name="Chosen")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="Older")
    await _pin(db, r1)
    r2 = await _seed_report(db, ta, ua, title="Newer")
    await _pin(db, r2)
    headers = make_auth_headers(ua)

    put_resp = await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r1.id)})
    assert put_resp.status_code == 200, put_resp.text

    resp = await client.get("/api/v1/dashboard", headers=headers)
    body = resp.json()
    assert body["active"]["id"] == str(r1.id)
    assert body["active_is_fallback"] is False


async def test_get_chosen_then_unpublished_falls_back_with_flag_true(client, db):
    ta = await create_test_tenant(db, name="ChosenThenUnpub")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="WillUnpublish")
    await _pin(db, r1)
    r2 = await _seed_report(db, ta, ua, title="StaysPublished")
    await _pin(db, r2)
    headers = make_auth_headers(ua)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r1.id)})
    ).status_code == 200

    # unpublish r1 directly (out of scope of this endpoint set; mirrors
    # reports.py's unpin_report mutation)
    r1.dashboard_pinned_at = None
    await db.flush()

    resp = await client.get("/api/v1/dashboard", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"]["id"] == str(r2.id)
    assert body["active_is_fallback"] is True

    # the stale preference row is NOT deleted on read (self-heal, not delete —
    # a report can be re-published)
    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one_or_none()
    assert pref is not None and pref.report_id == r1.id


async def test_get_chosen_then_deleted_falls_back_with_flag_true(client, db):
    """Deleting the selected report tombstones the preference row (report_id
    -> NULL via ON DELETE SET NULL, migration 092) instead of removing it —
    same fallback-notice contract as the unpublished case above."""
    ta = await create_test_tenant(db, name="ChosenThenDeleted")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="WillBeDeleted")
    await _pin(db, r1)
    r2 = await _seed_report(db, ta, ua, title="StaysPublished")
    await _pin(db, r2)
    headers = make_auth_headers(ua)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r1.id)})
    ).status_code == 200

    await db.delete(r1)
    await db.flush()

    resp = await client.get("/api/v1/dashboard", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"]["id"] == str(r2.id)
    assert body["active_is_fallback"] is True

    # the preference row survives as a tombstone, not deleted
    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one_or_none()
    assert pref is not None and pref.report_id is None


async def test_get_chosen_then_all_unpublished_falls_back_to_none(client, db):
    ta = await create_test_tenant(db, name="ChosenThenAllUnpub")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="Solo")
    await _pin(db, r1)
    headers = make_auth_headers(ua)
    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r1.id)})
    ).status_code == 200

    r1.dashboard_pinned_at = None
    await db.flush()

    resp = await client.get("/api/v1/dashboard", headers=headers)
    body = resp.json()
    assert body["published"] == []
    assert body["active"] is None
    # active_is_fallback is False when there is nothing to substitute: the
    # fallback banner reads "...showing {title} instead", which cannot render
    # with no report at all — Task 5's empty state takes over that surface
    # instead of a banner naming nothing.
    assert body["active_is_fallback"] is False


async def test_get_unauth_401(client, db):
    resp = await client.get("/api/v1/dashboard")
    assert resp.status_code == 401


# --- PUT /dashboard/active ---------------------------------------------------


async def test_put_active_upserts_and_audits(client, db):
    ta = await create_test_tenant(db, name="PutUpsert")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r = await _seed_report(db, ta, ua, title="Selectable")
    await _pin(db, r)
    headers = make_auth_headers(ua)

    resp = await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r.id)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"]["id"] == str(r.id)
    assert body["active_is_fallback"] is False

    audit = (
        await db.execute(
            text(
                "SELECT actor_id, actor_type, category, resource_id FROM audit_events "
                "WHERE action='dashboard.select' AND resource_id=:rid"
            ),
            {"rid": str(r.id)},
        )
    ).first()
    assert audit is not None
    assert audit[0] == ua.id and audit[1] == "user" and audit[2] == "dashboard"

    # only one preference row exists after the upsert
    count = (
        await db.execute(
            text("SELECT count(*) FROM user_dashboard_preferences WHERE tenant_id=:tid AND user_id=:uid"),
            {"tid": str(ta.id), "uid": str(ua.id)},
        )
    ).scalar_one()
    assert count == 1


async def test_put_active_switching_choice_updates_same_row(client, db):
    ta = await create_test_tenant(db, name="PutSwitch")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="First")
    await _pin(db, r1)
    r2 = await _seed_report(db, ta, ua, title="Second")
    await _pin(db, r2)
    headers = make_auth_headers(ua)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r1.id)})
    ).status_code == 200
    second = await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r2.id)})
    assert second.status_code == 200
    assert second.json()["active"]["id"] == str(r2.id)

    count = (
        await db.execute(
            text("SELECT count(*) FROM user_dashboard_preferences WHERE tenant_id=:tid AND user_id=:uid"),
            {"tid": str(ta.id), "uid": str(ua.id)},
        )
    ).scalar_one()
    assert count == 1  # upsert, not a second row


async def test_put_active_conflicting_row_upserts_instead_of_500(client, db):
    """Regression for the read-then-insert race: PUT must use a real ON
    CONFLICT upsert, not a SELECT-then-branch. Simulates a racing writer's
    INSERT having already landed by inserting the conflicting row directly,
    then asserts the endpoint's own upsert statement resolves via UPDATE
    (constraint uq_user_dashboard_preference_tenant_user) rather than
    attempting a second INSERT that would 500 with IntegrityError."""
    ta = await create_test_tenant(db, name="PutRace")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="First")
    await _pin(db, r1)
    r2 = await _seed_report(db, ta, ua, title="Second")
    await _pin(db, r2)
    headers = make_auth_headers(ua)

    # Simulate the racing writer landing first.
    db.add(UserDashboardPreference(tenant_id=ta.id, user_id=ua.id, report_id=r1.id))
    await db.flush()

    resp = await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r2.id)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["active"]["id"] == str(r2.id)

    count = (
        await db.execute(
            text("SELECT count(*) FROM user_dashboard_preferences WHERE tenant_id=:tid AND user_id=:uid"),
            {"tid": str(ta.id), "uid": str(ua.id)},
        )
    ).scalar_one()
    assert count == 1  # upsert resolved to a single row, not a duplicate/error


async def test_put_active_unpublished_report_is_409_exact_detail(client, db):
    ta = await create_test_tenant(db, name="PutUnpub")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r = await _seed_report(db, ta, ua, title="NeverPublished")  # dashboard_pinned_at stays NULL
    headers = make_auth_headers(ua)

    resp = await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r.id)})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "That report isn't published to the dashboard"


async def test_put_active_unknown_and_malformed_id_are_404(client, db):
    ta = await create_test_tenant(db, name="PutNotFound")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    headers = make_auth_headers(ua)

    unknown = await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(uuid.uuid4())})
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "Report not found"

    malformed = await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": "not-a-uuid"})
    assert malformed.status_code == 404
    assert malformed.json()["detail"] == "Report not found"


async def test_put_active_cross_tenant_report_is_404_even_without_rls(client, db):
    """Same defense-in-depth idiom as reports.py's I1 tests: the `db` fixture
    connects as the BYPASSRLS owner, so this proves `_get_visible_report`'s own
    tenant_id predicate, not RLS."""
    tenant_a = await create_test_tenant(db, name="PutCrossA")
    tenant_b = await create_test_tenant(db, name="PutCrossB")
    user_a, _ = await create_test_user(db, tenant_a)
    user_b, _ = await create_test_user(db, tenant_b)

    await set_tenant_context(db, str(tenant_b.id))
    r_b = await _seed_report(db, tenant_b, user_b, title="BelongsToB")
    await _pin(db, r_b)

    await set_tenant_context(db, str(tenant_a.id))
    resp = await client.put(
        "/api/v1/dashboard/active", headers=make_auth_headers(user_a), json={"report_id": str(r_b.id)}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Report not found"


async def test_put_active_unauth_401(client, db):
    ta = await create_test_tenant(db, name="PutUnauth")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r = await _seed_report(db, ta, ua)
    await _pin(db, r)

    resp = await client.put("/api/v1/dashboard/active", json={"report_id": str(r.id)})
    assert resp.status_code == 401


async def test_put_active_no_permission_gate_beyond_login(client, db):
    """A non-admin, non-creator tenant member may still choose a wallpaper —
    unlike report pin/unpin, this is a personal read preference, not a
    workspace-wide mutation."""
    ta = await create_test_tenant(db, name="PutAnyRole")
    creator, _ = await create_test_user(db, ta, role_name="admin")
    other, _ = await create_test_user(db, ta, email="other-wall@test.com", role_name="readonly")
    await set_tenant_context(db, str(ta.id))
    r = await _seed_report(db, ta, creator, title="AdminsReport")
    await _pin(db, r)

    resp = await client.put("/api/v1/dashboard/active", headers=make_auth_headers(other), json={"report_id": str(r.id)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["active"]["id"] == str(r.id)


# --- DELETE /dashboard/active -------------------------------------------------


async def test_delete_active_clears_selection_and_audits(client, db):
    ta = await create_test_tenant(db, name="DeleteClears")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="Older")
    await _pin(db, r1)
    r2 = await _seed_report(db, ta, ua, title="Newer")
    await _pin(db, r2)
    headers = make_auth_headers(ua)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r1.id)})
    ).status_code == 200

    resp = await client.delete("/api/v1/dashboard/active", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # cleared -> falls back to most recently published, and this is NOT the
    # "stale selection" fallback (no stored selection exists at all anymore)
    assert body["active"]["id"] == str(r2.id)
    assert body["active_is_fallback"] is False

    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one_or_none()
    assert pref is None

    audit = (
        await db.execute(
            text("SELECT count(*) FROM audit_events WHERE action='dashboard.clear' AND actor_id=:aid"),
            {"aid": str(ua.id)},
        )
    ).scalar_one()
    assert audit == 1


async def test_delete_active_is_idempotent(client, db):
    ta = await create_test_tenant(db, name="DeleteIdempotent")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    headers = make_auth_headers(ua)

    first = await client.delete("/api/v1/dashboard/active", headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["active"] is None

    second = await client.delete("/api/v1/dashboard/active", headers=headers)
    assert second.status_code == 200
    assert second.json()["active"] is None

    audit = (
        await db.execute(
            text("SELECT count(*) FROM audit_events WHERE action='dashboard.clear' AND actor_id=:aid"),
            {"aid": str(ua.id)},
        )
    ).scalar_one()
    assert audit == 2  # idempotent operations still each audit, matching unpin_report precedent


async def test_delete_active_unauth_401(client, db):
    ta = await create_test_tenant(db, name="DeleteUnauth")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))

    resp = await client.delete("/api/v1/dashboard/active")
    assert resp.status_code == 401
