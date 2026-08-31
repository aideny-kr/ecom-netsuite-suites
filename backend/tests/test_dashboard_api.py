# backend/tests/test_dashboard_api.py
"""GET /dashboard + PUT/DELETE /dashboard/active — the per-user wallpaper API.

Pattern mirrors test_reports_api.py's pin/unpin section: `_get_owned`-style
404 parity, flush/audit-then-commit ordering, `_to_response` UUID
stringification. `_get_visible_report` here is deliberately NOT ownership
gated (unlike reports.py's `_get_owned` + `_can_manage`) — any tenant member
may choose any published report as their own wallpaper."""

import uuid
from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.database import set_tenant_context
from app.models.report import Report
from app.models.report_series import ReportSeries
from app.models.user_dashboard_preference import UserDashboardPreference
from app.services.report.period_resolver import ClosedPeriod, PeriodUnavailableReason
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


# --- Rolling-period Stage 1 (Task 4): series selection helpers -------------


async def _seed_series(db, ta, ua, *, playbook_key: str = "income_statement") -> ReportSeries:
    s = ReportSeries(tenant_id=ta.id, playbook_key=playbook_key, created_by=ua.id)
    db.add(s)
    await db.flush()
    return s


async def _seed_tracking_report(db, ta, ua, series, period: str, *, title: str | None = None, created_at=None):
    """A report linked into a series (mode="tracking" compose, Task 3) — NOT pinned to
    `dashboard_pinned_at`: series membership, not the workspace-published-set pin, is
    what makes a tracking report reachable via its series."""
    r = Report(
        tenant_id=ta.id,
        title=title or f"{series.playbook_key} {period}",
        spec_json={"sections": []},
        rendered_html=f"<html>{period}</html>",
        created_by=ua.id,
        series_id=series.id,
        period=period,
    )
    db.add(r)
    await db.flush()
    if created_at is not None:
        # Override the server_default `created_at` directly, then re-flush — needed to
        # construct the "newest PERIOD composed FIRST" scenario a created_at sort would
        # get wrong (Task 4 brief's required test case).
        r.created_at = created_at
        await db.flush()
    return r


def _patch_resolver(monkeypatch, closed: ClosedPeriod):
    """Fake `resolve_last_closed_period`, mirroring test_report_playbooks.py's own
    helper of the same name (duplicated, not imported — each test file owns its own
    fixtures). Patched at the period_resolver module boundary, not
    app.api.v1.dashboard: the endpoint imports it lazily inside the function body, so a
    `from X import Y` executed at call time reads the CURRENT `X.Y` — patching the
    source module is what a monkeypatch set up before the call actually reaches."""

    async def fake_resolve(db, tenant_id):
        return closed

    monkeypatch.setattr("app.services.report.period_resolver.resolve_last_closed_period", fake_resolve)


_JUN_CLOSED = ClosedPeriod(name="Jun 2026", enddate=date(2026, 6, 30))
_JUN_CLOSED_JUL_OPEN = ClosedPeriod(
    name="Jun 2026", enddate=date(2026, 6, 30), next_open_name="Jul 2026", next_open_enddate=date(2026, 7, 31)
)
_UNREACHABLE = ClosedPeriod(name=None, enddate=None, reason=PeriodUnavailableReason.UNREACHABLE)


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

    # GET is pure (M-round-3 fix): it must NEVER write, so the tombstone row
    # survives untouched — only POST /dashboard/notice/dismiss clears it (see
    # the dismiss-endpoint tests below).
    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one_or_none()
    assert pref is not None and pref.report_id is None


async def test_get_deleted_report_tombstone_banner_persists_until_dismissed(client, db):
    """Round-3 T2-gate fix: GET must never mutate. A `PublishedDashboardsSection`
    on an unrelated page (`/reports`) shares the same `["dashboard"]` query key
    but only reads `published`/`active` — if GET silently consumed the
    tombstone as a side effect, visiting `/reports` before `/dashboard` would
    clear the notice before the user ever saw the banner. So the tombstone (and
    the True flag) must survive ANY number of repeated GETs; only an explicit
    POST /dashboard/notice/dismiss (tested below) may clear it."""
    ta = await create_test_tenant(db, name="TombstonePersists")
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

    for _ in range(3):
        resp = await client.get("/api/v1/dashboard", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["active_is_fallback"] is True
        assert resp.json()["active"]["id"] == str(r2.id)

    # GET never audits a mutation it never performed.
    cleared = (
        await db.execute(text("SELECT count(*) FROM audit_events WHERE action='dashboard.stale_selection_cleared'"))
    ).scalar_one()
    assert cleared == 0

    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one_or_none()
    assert pref is not None and pref.report_id is None


async def test_clear_stale_selection_survives_concurrent_republish(client, db):
    """TOCTOU for the unpublished-case branch of the generalized helper
    (FIX1): the predicate re-checks the LIVE `reports` table inside the
    DELETE statement itself, not a `published` snapshot taken before the
    race — so if the report gets re-published in the window between load and
    delete, the delete must not fire (the selection is valid again)."""
    from app.api.v1.dashboard import _clear_stale_selection

    ta = await create_test_tenant(db, name="ReconRepublishRace")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="WillRepublish")
    await _pin(db, r1)
    headers = make_auth_headers(ua)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r1.id)})
    ).status_code == 200
    r1.dashboard_pinned_at = None  # unpublish
    await db.flush()

    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one()

    # simulate a concurrent re-publish landing between load and delete
    await _pin(db, r1)

    cleared = await _clear_stale_selection(db, pref, ua)
    assert cleared is False
    await db.commit()

    survivor = (
        await db.execute(select(UserDashboardPreference).where(UserDashboardPreference.id == pref.id))
    ).scalar_one_or_none()
    assert survivor is not None
    assert survivor.report_id == r1.id  # the now-valid-again selection was NOT discarded

    audit_count = (
        await db.execute(text("SELECT count(*) FROM audit_events WHERE action='dashboard.stale_selection_cleared'"))
    ).scalar_one()
    assert audit_count == 0


async def test_tombstone_conditional_delete_survives_concurrent_repoint(client, db):
    """TOCTOU regression: `_get_preference` loads the tombstone (report_id
    NULL) before the GC delete runs. If, in that window, a concurrent
    PUT /dashboard/active on another connection re-points the SAME row at a
    real report (`set_active_dashboard`'s upsert updates report_id on the
    existing row via ON CONFLICT DO UPDATE, not a new INSERT), an
    unconditional-by-pk delete would silently discard that fresh selection
    along with the tombstone.

    The `client`/`db` fixtures share one connection/transaction (conftest.py),
    so a true two-connection interleave can't be reproduced through a single
    HTTP request. Instead this calls the module's conditional-delete-and-audit
    helper directly against a tombstone that has been re-pointed out from
    under it — exactly the state the real interleave would leave behind —
    and asserts the row survives with the concurrent selection intact."""
    from app.api.v1.dashboard import _clear_stale_selection

    ta = await create_test_tenant(db, name="ToctouRepoint")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="WillBeDeleted")
    await _pin(db, r1)
    r2 = await _seed_report(db, ta, ua, title="RepointTarget")
    await _pin(db, r2)
    headers = make_auth_headers(ua)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r1.id)})
    ).status_code == 200

    await db.delete(r1)
    await db.flush()

    # load the tombstone the same way _build_dashboard_response does
    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one()
    assert pref.report_id is None

    # simulate the concurrent PUT landing between load and delete: re-point
    # the SAME row at r2 (mirrors the upsert, which updates the existing row
    # rather than inserting a new one)
    pref.report_id = r2.id
    await db.flush()

    cleared = await _clear_stale_selection(db, pref, ua)
    assert cleared is False
    await db.commit()

    survivor = (
        await db.execute(select(UserDashboardPreference).where(UserDashboardPreference.id == pref.id))
    ).scalar_one_or_none()
    assert survivor is not None
    assert survivor.report_id == r2.id  # the concurrent selection was NOT discarded

    audit_count = (
        await db.execute(text("SELECT count(*) FROM audit_events WHERE action='dashboard.stale_selection_cleared'"))
    ).scalar_one()
    assert audit_count == 0  # a no-op delete must not audit a clear that didn't happen


async def test_tombstone_conditional_delete_second_racer_is_noop_no_duplicate_audit(client, db):
    """Two concurrent GETs both loading the same stale tombstone before either
    delete runs is the other half of the TOCTOU: the winner's DELETE matches
    the row, the loser's matches zero. The loser must not log a second
    dashboard.stale_selection_cleared event for what is documented — and tested end
    to end via test_get_deleted_report_tombstone_banner_fires_exactly_once —
    as a one-time notice. That existing test only exercises two *sequential*
    GETs (the tombstone is already gone by the second one); this test drives
    the actual race by calling the helper twice against one shared, already
    loaded `pref` object, the way two racing requests both would."""
    from app.api.v1.dashboard import _clear_stale_selection

    ta = await create_test_tenant(db, name="ToctouRacers")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="WillBeDeleted")
    await _pin(db, r1)
    headers = make_auth_headers(ua)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r1.id)})
    ).status_code == 200

    await db.delete(r1)
    await db.flush()

    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one()

    winner = await _clear_stale_selection(db, pref, ua)
    loser = await _clear_stale_selection(db, pref, ua)
    await db.commit()

    assert winner is True
    assert loser is False

    audit_count = (
        await db.execute(text("SELECT count(*) FROM audit_events WHERE action='dashboard.stale_selection_cleared'"))
    ).scalar_one()
    assert audit_count == 1

    survivor = (
        await db.execute(select(UserDashboardPreference).where(UserDashboardPreference.id == pref.id))
    ).scalar_one_or_none()
    assert survivor is None


async def test_get_unpublished_report_tombstone_survives_repeated_gets(client, db):
    """The self-heal-and-delete behavior above is scoped to a DELETED report's
    tombstone (report_id IS NULL) only. An unpublished-but-still-existing
    selection must survive every repeated get untouched — the report can be
    re-published, so the row keeps remembering the user's choice."""
    ta = await create_test_tenant(db, name="UnpubSurvivesRepeats")
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

    r1.dashboard_pinned_at = None
    await db.flush()

    for _ in range(3):
        resp = await client.get("/api/v1/dashboard", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["active_is_fallback"] is True
        assert resp.json()["active"]["id"] == str(r2.id)

    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one_or_none()
    assert pref is not None and pref.report_id == r1.id


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


async def test_get_published_order_is_deterministic_when_pinned_at_ties(client, db):
    """M6: `ORDER BY dashboard_pinned_at DESC` alone has no tiebreaker, so two (or
    more) reports published in the same tick could flip order between requests —
    and the fallback is `published[0]`, so the fallback itself was nondeterministic.
    Assert a stable id-DESC secondary sort when timestamps are identical."""
    ta = await create_test_tenant(db, name="TieBreak")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    from datetime import datetime, timezone

    same_ts = datetime.now(timezone.utc)
    reports = []
    for i in range(5):
        r = await _seed_report(db, ta, ua, title=f"Tied-{i}")
        r.dashboard_pinned_at = same_ts
        reports.append(r)
    await db.flush()
    headers = make_auth_headers(ua)

    resp = await client.get("/api/v1/dashboard", headers=headers)
    assert resp.status_code == 200, resp.text
    order = [row["id"] for row in resp.json()["published"]]
    expected = sorted((str(r.id) for r in reports), reverse=True)
    assert order == expected


async def test_get_published_reports_excludes_cross_tenant_reports_even_without_rls(client, db):
    """M7a: same defense-in-depth idiom as
    test_put_active_cross_tenant_report_is_404_even_without_rls — the `db` fixture
    connects as the BYPASSRLS owner, so this proves `_published_reports`' own
    explicit tenant_id predicate keeps another tenant's published reports out of
    `published`, not RLS."""
    tenant_a = await create_test_tenant(db, name="GetCrossA")
    tenant_b = await create_test_tenant(db, name="GetCrossB")
    user_a, _ = await create_test_user(db, tenant_a)
    user_b, _ = await create_test_user(db, tenant_b)

    await set_tenant_context(db, str(tenant_b.id))
    r_b = await _seed_report(db, tenant_b, user_b, title="BelongsToB")
    await _pin(db, r_b)

    await set_tenant_context(db, str(tenant_a.id))
    r_a = await _seed_report(db, tenant_a, user_a, title="BelongsToA")
    await _pin(db, r_a)

    resp = await client.get("/api/v1/dashboard", headers=make_auth_headers(user_a))
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()["published"]}
    assert ids == {str(r_a.id)}
    assert str(r_b.id) not in ids


# --- POST /dashboard/notice/dismiss ------------------------------------------
#
# Round-3 T2-gate fix: the GET path above no longer mutates, so the deleted-
# report tombstone (report_id -> NULL) is only ever cleared by an explicit
# user action against this endpoint — never as a side effect of a read.


async def test_dismiss_clears_tombstone_and_audits_once(client, db):
    ta = await create_test_tenant(db, name="DismissClears")
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

    # Confirm the tombstone survives an ordinary GET first (round-3 invariant).
    pre = await client.get("/api/v1/dashboard", headers=headers)
    assert pre.json()["active_is_fallback"] is True

    resp = await client.post("/api/v1/dashboard/notice/dismiss", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Same post-clear shape as DELETE /active: no stored selection left, so
    # falling back to the most recent publish is NOT "your old choice vanished"
    # anymore — there IS no old choice anymore.
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

    cleared = (
        await db.execute(text("SELECT count(*) FROM audit_events WHERE action='dashboard.stale_selection_cleared'"))
    ).scalar_one()
    assert cleared == 1

    # And a subsequent GET now reports a clean "never chose" fallback.
    post = await client.get("/api/v1/dashboard", headers=headers)
    assert post.json()["active_is_fallback"] is False
    assert post.json()["active"]["id"] == str(r2.id)


async def test_dismiss_is_idempotent_second_call_is_noop(client, db):
    ta = await create_test_tenant(db, name="DismissIdempotent")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="WillBeDeleted")
    await _pin(db, r1)
    headers = make_auth_headers(ua)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r1.id)})
    ).status_code == 200
    await db.delete(r1)
    await db.flush()

    first = await client.post("/api/v1/dashboard/notice/dismiss", headers=headers)
    assert first.status_code == 200, first.text

    second = await client.post("/api/v1/dashboard/notice/dismiss", headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["active_is_fallback"] is False

    cleared = (
        await db.execute(text("SELECT count(*) FROM audit_events WHERE action='dashboard.stale_selection_cleared'"))
    ).scalar_one()
    assert cleared == 1  # not audited again on the second, no-op call


async def test_dismiss_noop_when_no_preference_exists(client, db):
    """No stored selection at all — a user who never chose anything, or one
    who is simply revisiting `/dashboard` and the FE fires a dismiss it
    doesn't strictly need to. Must 200, not error, and must not audit."""
    ta = await create_test_tenant(db, name="DismissNoPref")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="Solo")
    await _pin(db, r1)
    headers = make_auth_headers(ua)

    resp = await client.post("/api/v1/dashboard/notice/dismiss", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["active"]["id"] == str(r1.id)
    assert resp.json()["active_is_fallback"] is False

    cleared = (
        await db.execute(text("SELECT count(*) FROM audit_events WHERE action='dashboard.stale_selection_cleared'"))
    ).scalar_one()
    assert cleared == 0


async def test_dismiss_clears_unpublished_selection_and_audits(client, db):
    """FIX1: `active_is_fallback` has two triggers, per `_resolve_active`'s own
    docstring — the selected report was deleted (report_id NULL, the
    tombstone case above) or unpublished (report_id still points at a real,
    now-unpublished report). Before this fix, dismiss only handled the first:
    `_clear_tombstone_if_still_null`'s `report_id IS NULL` predicate never
    matched the unpublished case, so the dismiss button silently no-opped and
    the banner reappeared on every subsequent load with no way to silence it.
    The generalized `_clear_stale_selection` must clear this case too."""
    ta = await create_test_tenant(db, name="DismissClearsUnpub")
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
    r1.dashboard_pinned_at = None  # unpublish, not delete
    await db.flush()

    pre = await client.get("/api/v1/dashboard", headers=headers)
    assert pre.json()["active_is_fallback"] is True

    resp = await client.post("/api/v1/dashboard/notice/dismiss", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"]["id"] == str(r2.id)
    assert body["active_is_fallback"] is False

    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one_or_none()
    assert pref is None  # the stale selection row is gone, not merely self-healed

    cleared = (
        await db.execute(text("SELECT count(*) FROM audit_events WHERE action='dashboard.stale_selection_cleared'"))
    ).scalar_one()
    assert cleared == 1

    # The audit row must be filed like this module's other dashboard mutations
    # (dashboard.select / dashboard.clear): category="dashboard" so an operator
    # filtering GET /audit?category=dashboard sees the whole family, and
    # resource_id = the REPORT's id — not the preference row's PK, which would
    # never resolve against resource_type="report".
    cat, rid = (
        await db.execute(
            text("SELECT category, resource_id FROM audit_events WHERE action='dashboard.stale_selection_cleared'")
        )
    ).one()
    assert cat == "dashboard"
    assert rid == str(r1.id)

    # the banner must NOT reappear on the next load
    post = await client.get("/api/v1/dashboard", headers=headers)
    assert post.json()["active_is_fallback"] is False
    assert post.json()["active"]["id"] == str(r2.id)


async def test_dismiss_unpublished_case_is_idempotent_second_call_is_noop(client, db):
    ta = await create_test_tenant(db, name="DismissUnpubIdempotent")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="WillUnpublish")
    await _pin(db, r1)
    headers = make_auth_headers(ua)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r1.id)})
    ).status_code == 200
    r1.dashboard_pinned_at = None
    await db.flush()

    first = await client.post("/api/v1/dashboard/notice/dismiss", headers=headers)
    assert first.status_code == 200, first.text

    second = await client.post("/api/v1/dashboard/notice/dismiss", headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["active_is_fallback"] is False

    cleared = (
        await db.execute(text("SELECT count(*) FROM audit_events WHERE action='dashboard.stale_selection_cleared'"))
    ).scalar_one()
    assert cleared == 1  # not audited again on the second, no-op call


async def test_dismiss_does_not_clear_a_currently_valid_selection(client, db):
    """Dismiss must be scoped to STALE selections only. A normal, currently-
    published choice must survive a dismiss call untouched — e.g. an FE that
    fires dismiss defensively on every load even when there is nothing stale
    to dismiss must never destroy a live selection."""
    ta = await create_test_tenant(db, name="DismissKeepsValid")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="Chosen")
    await _pin(db, r1)
    headers = make_auth_headers(ua)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r1.id)})
    ).status_code == 200

    resp = await client.post("/api/v1/dashboard/notice/dismiss", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["active"]["id"] == str(r1.id)
    assert resp.json()["active_is_fallback"] is False

    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one_or_none()
    assert pref is not None and pref.report_id == r1.id  # untouched

    cleared = (
        await db.execute(text("SELECT count(*) FROM audit_events WHERE action='dashboard.stale_selection_cleared'"))
    ).scalar_one()
    assert cleared == 0


async def test_dismiss_unauth_401(client, db):
    resp = await client.post("/api/v1/dashboard/notice/dismiss")
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
    """FIX3: the `dashboard.clear` audit must be gated on an actual deletion —
    a repeat/idempotent DELETE with no preference to clear (never chosen
    anything, so there is nothing to delete on either call) must return 200
    without writing a phantom audit row claiming a clear that didn't happen."""
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
    assert audit == 0  # no preference ever existed — nothing was deleted, so nothing is audited


async def test_delete_active_second_call_after_real_clear_is_noop_no_audit(client, db):
    """Distinguishes FIX3's audit-gating from the old "always audit" behavior:
    the FIRST delete, which actually clears a real preference, must still
    audit exactly once; only the redundant SECOND call (nothing left to
    delete) must stay silent."""
    ta = await create_test_tenant(db, name="DeleteRealThenNoop")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="Chosen")
    await _pin(db, r1)
    headers = make_auth_headers(ua)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r1.id)})
    ).status_code == 200

    first = await client.delete("/api/v1/dashboard/active", headers=headers)
    assert first.status_code == 200, first.text

    second = await client.delete("/api/v1/dashboard/active", headers=headers)
    assert second.status_code == 200, second.text

    audit = (
        await db.execute(
            text("SELECT count(*) FROM audit_events WHERE action='dashboard.clear' AND actor_id=:aid"),
            {"aid": str(ua.id)},
        )
    ).scalar_one()
    assert audit == 1  # the real clear audited once; the redundant repeat did not


async def test_delete_active_conditional_survives_concurrent_repoint(client, db):
    """FIX2: `clear_active_dashboard`'s DELETE must not be an unconditional
    `db.delete(pref)` by primary key — a concurrent PUT can re-point the SAME
    row (the upsert in `set_active_dashboard` updates the existing row via
    `ON CONFLICT DO UPDATE`, not a new INSERT) between this row being loaded
    and this delete executing, and an unconditional delete would silently
    discard that fresh selection. Same conditional-delete idiom as
    `_clear_stale_selection`, but re-asserting the captured `report_id` value
    rather than checking "is it stale" — an explicit user DELETE has no
    staleness question to ask, it means "forget whatever is there."

    The `client`/`db` fixtures share one connection/transaction (conftest.py),
    so this drives the race the same way the tombstone TOCTOU tests above do:
    call the module's conditional-delete helper directly against a row that
    has been re-pointed out from under it."""
    from app.api.v1.dashboard import _delete_preference_if_unchanged

    ta = await create_test_tenant(db, name="DeleteRepointRace")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r1 = await _seed_report(db, ta, ua, title="Original")
    await _pin(db, r1)
    r2 = await _seed_report(db, ta, ua, title="RepointTarget")
    await _pin(db, r2)
    headers = make_auth_headers(ua)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r1.id)})
    ).status_code == 200

    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one()
    assert pref.report_id == r1.id

    # Simulate the concurrent PUT landing between load and delete: re-point
    # the SAME row at r2 via a raw UPDATE that bypasses this ORM object,
    # exactly like a concurrent writer on a SEPARATE connection would — so
    # `pref` (this request's in-memory snapshot) keeps holding the value it
    # was loaded with (r1.id) while the actual row now holds r2.id. Mutating
    # `pref.report_id` directly, as the tombstone TOCTOU tests above do,
    # would be self-defeating here: `_delete_preference_if_unchanged`
    # re-asserts equality against `pref.report_id` itself, so comparing the
    # object against its own just-mutated attribute would trivially match.
    await db.execute(
        text("UPDATE user_dashboard_preferences SET report_id = :rid WHERE id = :id"),
        {"rid": str(r2.id), "id": str(pref.id)},
    )
    await db.flush()

    cleared = await _delete_preference_if_unchanged(db, pref)
    assert cleared is False
    await db.commit()

    survivor = (
        await db.execute(select(UserDashboardPreference).where(UserDashboardPreference.id == pref.id))
    ).scalar_one_or_none()
    assert survivor is not None
    assert survivor.report_id == r2.id  # the concurrent selection was NOT discarded

    audit_count = (
        await db.execute(text("SELECT count(*) FROM audit_events WHERE action='dashboard.clear'"))
    ).scalar_one()
    assert audit_count == 0  # a no-op delete must not audit a clear that didn't happen


async def test_delete_active_unauth_401(client, db):
    ta = await create_test_tenant(db, name="DeleteUnauth")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))

    resp = await client.delete("/api/v1/dashboard/active")
    assert resp.status_code == 401


# --- Rolling-period Stage 1 (Task 4): the wall follows a series -------------


async def test_put_active_series_resolves_to_newest_report_by_period_not_created_at(client, db, monkeypatch):
    """The required Task 4 test case: the newest PERIOD ("Mar 2026") was composed
    FIRST (oldest created_at) -- a created_at sort would pick "Jan 2026" instead. Only
    a chronological period sort gets this right."""
    ta = await create_test_tenant(db, name="SeriesChronological")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    from datetime import datetime, timedelta, timezone

    series = await _seed_series(db, ta, ua)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    r_mar = await _seed_tracking_report(db, ta, ua, series, "Mar 2026", created_at=base)
    await _seed_tracking_report(db, ta, ua, series, "Jan 2026", created_at=base + timedelta(days=1))
    await _seed_tracking_report(db, ta, ua, series, "Feb 2026", created_at=base + timedelta(days=2))
    headers = make_auth_headers(ua)
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    resp = await client.put("/api/v1/dashboard/active", headers=headers, json={"series_id": str(series.id)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"]["id"] == str(r_mar.id)
    assert body["active"]["period"] == "Mar 2026"
    assert body["active_is_fallback"] is False

    get_resp = await client.get("/api/v1/dashboard", headers=headers)
    assert get_resp.json()["active"]["id"] == str(r_mar.id)


async def test_put_and_get_series_no_reports_yet_is_sane_empty_shape_not_500(client, db, monkeypatch):
    ta = await create_test_tenant(db, name="SeriesEmpty")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    series = await _seed_series(db, ta, ua)
    headers = make_auth_headers(ua)

    put_resp = await client.put("/api/v1/dashboard/active", headers=headers, json={"series_id": str(series.id)})
    assert put_resp.status_code == 200, put_resp.text
    body = put_resp.json()
    assert body["active"] is None
    assert body["active_is_fallback"] is False
    assert body["active_tracking"]["series_id"] == str(series.id)
    assert body["active_tracking"]["period"] is None
    assert body["active_tracking"]["period_check_ok"] is False

    get_resp = await client.get("/api/v1/dashboard", headers=headers)
    assert get_resp.status_code == 200, get_resp.text
    get_body = get_resp.json()
    assert get_body["active"] is None
    assert get_body["active_is_fallback"] is False
    assert get_body["active_tracking"]["series_id"] == str(series.id)


async def test_get_series_selection_resolver_failure_degrades_not_500(client, db, monkeypatch):
    ta = await create_test_tenant(db, name="SeriesResolverDown")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    series = await _seed_series(db, ta, ua)
    r = await _seed_tracking_report(db, ta, ua, series, "Jun 2026")
    headers = make_auth_headers(ua)

    _patch_resolver(monkeypatch, _JUN_CLOSED)
    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"series_id": str(series.id)})
    ).status_code == 200

    _patch_resolver(monkeypatch, _UNREACHABLE)
    # T2-gate MAJOR 2: the PUT above cached the successful JUN_CLOSED resolution for
    # this tenant. Without clearing it, the GET below would legitimately reuse that
    # cached answer instead of calling the resolver again -- which is the whole point
    # of the cache, but it would mask what THIS test exists to exercise (a resolver
    # failure degrading gracefully). Clearing simulates the TTL having elapsed.
    from app.services.report.period_resolver import clear_period_cache

    clear_period_cache()
    resp = await client.get("/api/v1/dashboard", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"]["id"] == str(r.id)
    assert body["active_tracking"]["period_check_ok"] is False
    assert body["active_tracking"]["period"] == "Jun 2026"
    assert body["active_tracking"]["resolved_period"] is None
    assert body["active_tracking"]["next_open_period"] is None


async def test_get_series_selection_reports_resolved_period_and_next_open(client, db, monkeypatch):
    ta = await create_test_tenant(db, name="SeriesGreenRibbon")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    series = await _seed_series(db, ta, ua)
    await _seed_tracking_report(db, ta, ua, series, "Jun 2026")
    headers = make_auth_headers(ua)

    _patch_resolver(monkeypatch, _JUN_CLOSED_JUL_OPEN)
    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"series_id": str(series.id)})
    ).status_code == 200

    resp = await client.get("/api/v1/dashboard", headers=headers)
    body = resp.json()
    assert body["active_tracking"]["period_check_ok"] is True
    assert body["active_tracking"]["resolved_period"] == "Jun 2026"
    assert body["active_tracking"]["next_open_period"] == "Jul 2026"


async def test_active_tracking_is_absent_for_a_report_selection(client, db):
    """Mock section 3: "a pinned snapshot shows no ribbon" -- active_tracking must be
    entirely absent (None), not merely empty, for an ordinary report selection."""
    ta = await create_test_tenant(db, name="NoRibbonForSnapshot")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r = await _seed_report(db, ta, ua)
    await _pin(db, r)
    headers = make_auth_headers(ua)

    put_resp = await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r.id)})
    assert put_resp.json()["active_tracking"] is None

    get_resp = await client.get("/api/v1/dashboard", headers=headers)
    assert get_resp.json()["active_tracking"] is None


async def test_put_active_both_ids_is_400(client, db):
    ta = await create_test_tenant(db, name="BothIds")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r = await _seed_report(db, ta, ua)
    await _pin(db, r)
    series = await _seed_series(db, ta, ua)
    headers = make_auth_headers(ua)

    resp = await client.put(
        "/api/v1/dashboard/active", headers=headers, json={"report_id": str(r.id), "series_id": str(series.id)}
    )
    assert resp.status_code == 400


async def test_put_active_neither_id_is_400(client, db):
    ta = await create_test_tenant(db, name="NeitherId")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    headers = make_auth_headers(ua)

    resp = await client.put("/api/v1/dashboard/active", headers=headers, json={})
    assert resp.status_code == 400


async def test_put_active_unknown_and_malformed_series_id_are_404(client, db):
    ta = await create_test_tenant(db, name="SeriesNotFound")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    headers = make_auth_headers(ua)

    unknown = await client.put("/api/v1/dashboard/active", headers=headers, json={"series_id": str(uuid.uuid4())})
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "Series not found"

    malformed = await client.put("/api/v1/dashboard/active", headers=headers, json={"series_id": "not-a-uuid"})
    assert malformed.status_code == 404
    assert malformed.json()["detail"] == "Series not found"


async def test_put_active_cross_tenant_series_is_404_even_without_rls(client, db):
    tenant_a = await create_test_tenant(db, name="SeriesCrossA")
    tenant_b = await create_test_tenant(db, name="SeriesCrossB")
    user_a, _ = await create_test_user(db, tenant_a)
    user_b, _ = await create_test_user(db, tenant_b)

    await set_tenant_context(db, str(tenant_b.id))
    series_b = await _seed_series(db, tenant_b, user_b)

    await set_tenant_context(db, str(tenant_a.id))
    resp = await client.put(
        "/api/v1/dashboard/active", headers=make_auth_headers(user_a), json={"series_id": str(series_b.id)}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Series not found"


async def test_put_active_switching_report_to_series_clears_report_id(client, db, monkeypatch):
    ta = await create_test_tenant(db, name="SwitchReportToSeries")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r = await _seed_report(db, ta, ua)
    await _pin(db, r)
    series = await _seed_series(db, ta, ua)
    headers = make_auth_headers(ua)
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r.id)})
    ).status_code == 200
    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"series_id": str(series.id)})
    ).status_code == 200

    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one()
    assert pref.report_id is None
    assert pref.series_id == series.id


async def test_put_active_switching_series_to_report_clears_series_id(client, db, monkeypatch):
    ta = await create_test_tenant(db, name="SwitchSeriesToReport")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    series = await _seed_series(db, ta, ua)
    r = await _seed_report(db, ta, ua)
    await _pin(db, r)
    headers = make_auth_headers(ua)
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"series_id": str(series.id)})
    ).status_code == 200
    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"report_id": str(r.id)})
    ).status_code == 200

    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one()
    assert pref.series_id is None
    assert pref.report_id == r.id


async def test_check_constraint_bites_both_ids_set_directly(db):
    """DB-level proof (not just the API's 400) that
    ck_user_dashboard_preference_one_selection exists and is enforced -- a direct
    ORM write bypassing the endpoint entirely must still fail."""
    ta = await create_test_tenant(db, name="CheckConstraintBites")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    r = await _seed_report(db, ta, ua)
    series = await _seed_series(db, ta, ua)

    pref = UserDashboardPreference(tenant_id=ta.id, user_id=ua.id, report_id=r.id, series_id=series.id)
    db.add(pref)
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_published_series_lists_tenant_series_with_current_period_meta(client, db):
    ta = await create_test_tenant(db, name="PublishedSeriesList")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    series = await _seed_series(db, ta, ua, playbook_key="balance_sheet")
    r = await _seed_tracking_report(db, ta, ua, series, "May 2026")
    headers = make_auth_headers(ua)

    resp = await client.get("/api/v1/dashboard", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    entry = next(s for s in body["published_series"] if s["id"] == str(series.id))
    assert entry["playbook_key"] == "balance_sheet"
    assert entry["period"] == "May 2026"
    assert entry["report_id"] == str(r.id)


async def test_published_series_empty_series_has_null_period_and_report_id(client, db):
    ta = await create_test_tenant(db, name="PublishedSeriesEmptyEntry")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    series = await _seed_series(db, ta, ua)
    headers = make_auth_headers(ua)

    resp = await client.get("/api/v1/dashboard", headers=headers)
    body = resp.json()
    entry = next(s for s in body["published_series"] if s["id"] == str(series.id))
    assert entry["period"] is None
    assert entry["report_id"] is None


async def test_dismiss_does_not_clear_a_valid_series_selection(client, db, monkeypatch):
    """Regression: `_clear_stale_selection`'s original predicate treated ANY row with
    report_id IS NULL as stale (true before Task 4, when report_id NULL only ever meant
    "tombstoned"). Task 4 makes report_id NULL the NORMAL state for a valid series
    selection too -- dismiss must not delete it."""
    ta = await create_test_tenant(db, name="DismissSpareSeries")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    series = await _seed_series(db, ta, ua)
    await _seed_tracking_report(db, ta, ua, series, "Jun 2026")
    headers = make_auth_headers(ua)
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"series_id": str(series.id)})
    ).status_code == 200

    dismiss_resp = await client.post("/api/v1/dashboard/notice/dismiss", headers=headers)
    assert dismiss_resp.status_code == 200, dismiss_resp.text
    assert dismiss_resp.json()["active"]["period"] == "Jun 2026"

    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one_or_none()
    assert pref is not None
    assert pref.series_id == series.id


async def test_get_tombstoned_series_selection_falls_back_with_flag_true(client, db, monkeypatch):
    """Simulates `ON DELETE SET NULL` firing on the series FK (no series-delete
    endpoint exists yet to trigger this for real) -- same self-heal-to-most-recent-
    published-report story migration 092 already tells for a deleted report."""
    ta = await create_test_tenant(db, name="SeriesTombstoned")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    series = await _seed_series(db, ta, ua)
    await _seed_tracking_report(db, ta, ua, series, "Jun 2026")
    fallback_report = await _seed_report(db, ta, ua, title="Fallback")
    await _pin(db, fallback_report)
    headers = make_auth_headers(ua)
    _patch_resolver(monkeypatch, _JUN_CLOSED)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"series_id": str(series.id)})
    ).status_code == 200

    pref = (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == ta.id, UserDashboardPreference.user_id == ua.id
            )
        )
    ).scalar_one()
    pref.series_id = None  # simulate the FK's ON DELETE SET NULL
    await db.flush()

    resp = await client.get("/api/v1/dashboard", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"]["id"] == str(fallback_report.id)
    assert body["active_is_fallback"] is True
    assert body["active_tracking"] is None


async def test_put_active_series_resolver_runs_before_commit_sees_tenant_context(client, db, monkeypatch):
    """Regression for the mid-run-commit GUC-clearing failure mode (memory:
    set_local_tenant_context_mid_commit -- bitten twice in this program already): the
    resolver call touches an RLS-protected table (netsuite_suiteql.execute selects
    Connection), so it MUST run before `db.commit()`, not after, or it would silently
    see zero rows / report unreachable regardless of what's actually there."""
    ta = await create_test_tenant(db, name="ResolverBeforeCommit")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    series = await _seed_series(db, ta, ua)
    await _seed_tracking_report(db, ta, ua, series, "Jun 2026")
    headers = make_auth_headers(ua)

    seen = {}

    async def fake_resolve(db_arg, tenant_id):
        row = (await db_arg.execute(text("SELECT current_setting('app.current_tenant_id', true)"))).scalar_one()
        seen["tenant_ctx"] = row
        return _JUN_CLOSED

    monkeypatch.setattr("app.services.report.period_resolver.resolve_last_closed_period", fake_resolve)

    resp = await client.put("/api/v1/dashboard/active", headers=headers, json={"series_id": str(series.id)})
    assert resp.status_code == 200, resp.text
    assert seen["tenant_ctx"] == str(ta.id)


# --- T2-gate fixes: tenant-context restore (defense in depth) + period-close cache --


async def test_put_active_series_reestablishes_context_before_upsert_defense_in_depth(client, db, monkeypatch):
    """MAJOR 1 defense-in-depth (T2 gate): set_active_dashboard's series branch must
    re-set the tenant context itself immediately before its own RLS-protected write,
    not rely solely on resolve_last_closed_period's own restore (period_resolver.py's
    `finally` — see test_report_period_resolver.py's unit-level coverage of that) —
    mirrors playbooks.py's own "tool calls may commit" re-set before its Report
    insert. Asserted via CALL ORDERING (spy), the same idiom test_report_refresh.py
    uses for the identical GUC-survives-commit constraint: the `db` test fixture wraps
    everything in an outer transaction/savepoint, so a service-level db.commit() here
    is RELEASE SAVEPOINT, not a real COMMIT, and (per conftest.py's own documented
    caveat) does NOT clear SET LOCAL the way production does — asserting actual GUC
    state across a commit in this fixture would pass identically whether or not the
    guard exists at all."""
    ta = await create_test_tenant(db, name="DefenseInDepthOrdering")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    series = await _seed_series(db, ta, ua)
    await _seed_tracking_report(db, ta, ua, series, "Jun 2026")
    headers = make_auth_headers(ua)

    events: list[str] = []

    async def spy_resolve(db_arg, tenant_id):
        events.append("resolve")
        return _JUN_CLOSED

    real_commit = db.commit

    async def spy_commit():
        events.append("commit")
        await real_commit()

    async def spy_ctx(session, tenant_id):
        events.append("ctx")
        validated = str(uuid.UUID(str(tenant_id)))
        await session.execute(text(f"SET LOCAL app.current_tenant_id = '{validated}'"))

    monkeypatch.setattr("app.services.report.period_resolver.resolve_last_closed_period", spy_resolve)
    monkeypatch.setattr("app.api.v1.dashboard.set_tenant_context", spy_ctx)
    monkeypatch.setattr(db, "commit", spy_commit)

    resp = await client.put("/api/v1/dashboard/active", headers=headers, json={"series_id": str(series.id)})
    assert resp.status_code == 200, resp.text

    assert "resolve" in events and "ctx" in events and "commit" in events
    resolve_idx = events.index("resolve")
    ctx_idx = events.index("ctx")
    commit_idx = events.index("commit")
    assert resolve_idx < ctx_idx < commit_idx, f"expected resolve -> ctx -> commit ordering, got {events}"


async def test_get_dashboard_reuses_cached_period_resolution_within_ttl(client, db, monkeypatch):
    """MAJOR 2 (T2 gate): GET /dashboard (and PUT/dismiss) must not re-query NetSuite
    on every load for the same tracking selection — resolve_last_closed_period is
    called at most once across N requests within the cache TTL. If _build_tracking_info
    called the raw (uncached) resolver, this would be N calls, not 1."""
    from app.services.report.period_resolver import clear_period_cache

    clear_period_cache()
    ta = await create_test_tenant(db, name="DashboardCacheReuse")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    series = await _seed_series(db, ta, ua)
    await _seed_tracking_report(db, ta, ua, series, "Jun 2026")
    headers = make_auth_headers(ua)

    call_count = {"n": 0}

    async def counting_resolve(db_arg, tenant_id):
        call_count["n"] += 1
        return _JUN_CLOSED

    monkeypatch.setattr("app.services.report.period_resolver.resolve_last_closed_period", counting_resolve)

    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"series_id": str(series.id)})
    ).status_code == 200
    assert call_count["n"] == 1

    for _ in range(3):
        resp = await client.get("/api/v1/dashboard", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["active_tracking"]["period_check_ok"] is True

    assert call_count["n"] == 1  # every subsequent GET served the cached resolution


# --- Stage 2: the "waiting" ribbon's data is gated on a compose actually being scheduled ---


async def _behind_series_headers(client, db, monkeypatch, name):
    """A series whose newest report is May while NetSuite's last closed period is Jun —
    i.e. genuinely behind, the only situation the waiting ribbon describes."""
    ta = await create_test_tenant(db, name=name)
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    series = await _seed_series(db, ta, ua)
    await _seed_tracking_report(db, ta, ua, series, "May 2026")
    headers = make_auth_headers(ua)
    _patch_resolver(monkeypatch, _JUN_CLOSED)
    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"series_id": str(series.id)})
    ).status_code == 200
    return headers


async def test_closed_days_ago_is_withheld_when_the_scheduled_compose_is_disabled(client, db, monkeypatch):
    """The ribbon this field drives tells the user a statement "is scheduled". With
    ROLLING_PERIOD_AUTO_COMPOSE_ENABLED false, NOTHING is scheduled — no sweep will ever
    compose it — so sending the field would make the product promise work that never
    happens. That is precisely the defect the T2 gate caught in the Stage 1 launcher copy
    ("composed automatically when the next period closes", with no scheduler behind it).
    Gate the DATA, not merely the wording: wording drifts, a withheld field cannot lie.
    """
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ROLLING_PERIOD_AUTO_COMPOSE_ENABLED", False)
    headers = await _behind_series_headers(client, db, monkeypatch, "RibbonDisabled")

    body = (await client.get("/api/v1/dashboard", headers=headers)).json()
    assert body["active_tracking"]["period"] == "May 2026"
    assert body["active_tracking"]["resolved_period"] == "Jun 2026"
    assert body["active_tracking"]["closed_days_ago"] is None


async def test_closed_days_ago_is_sent_when_behind_and_the_sweep_is_enabled(client, db, monkeypatch):
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ROLLING_PERIOD_AUTO_COMPOSE_ENABLED", True)
    headers = await _behind_series_headers(client, db, monkeypatch, "RibbonEnabled")

    body = (await client.get("/api/v1/dashboard", headers=headers)).json()
    assert body["active_tracking"]["resolved_period"] == "Jun 2026"
    assert isinstance(body["active_tracking"]["closed_days_ago"], int)
    assert body["active_tracking"]["closed_days_ago"] >= 0


async def test_closed_days_ago_is_withheld_when_the_series_is_already_current(client, db, monkeypatch):
    """Caught up is the GREEN ribbon's job. Sending the field here would render a
    "waiting" state over a series that is not waiting for anything."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ROLLING_PERIOD_AUTO_COMPOSE_ENABLED", True)
    ta = await create_test_tenant(db, name="RibbonCurrent")
    ua, _ = await create_test_user(db, ta)
    await set_tenant_context(db, str(ta.id))
    series = await _seed_series(db, ta, ua)
    await _seed_tracking_report(db, ta, ua, series, "Jun 2026")
    headers = make_auth_headers(ua)
    _patch_resolver(monkeypatch, _JUN_CLOSED)
    assert (
        await client.put("/api/v1/dashboard/active", headers=headers, json={"series_id": str(series.id)})
    ).status_code == 200

    body = (await client.get("/api/v1/dashboard", headers=headers)).json()
    assert body["active_tracking"]["closed_days_ago"] is None
