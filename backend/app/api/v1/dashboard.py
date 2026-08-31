import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db, set_tenant_context
from app.core.dependencies import get_current_user
from app.models.report import Report
from app.models.report_series import ReportSeries
from app.models.user import User
from app.models.user_dashboard_preference import UserDashboardPreference
from app.schemas.dashboard import (
    DashboardActiveRequest,
    DashboardResponse,
    DashboardSeriesResponse,
    DashboardTrackingInfo,
)
from app.schemas.report import ReportResponse
from app.services import audit_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
logger = logging.getLogger(__name__)

# Rolling-period Stage 1 (Task 4): "Mon YYYY" -> (year, month) for a CHRONOLOGICAL sort
# over Report.period -- sorting the string itself is lexical ("Apr 2026" < "Jan 2026"),
# which is wrong (see the module docstring in period_resolver.py and the Task 4 brief).
# Every tracking-mode report's `period` is written by compose_playbook_report from
# period_resolver's already-validated "Mon YYYY" name, so this should always parse.
_MONTH_ABBRS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# No permission gate beyond get_current_user on ANY route in this module: choosing
# your own dashboard wallpaper is a personal preference, not a workspace-wide
# mutation (unlike report pin/unpin in reports.py, which changes the published set
# and IS creator-or-admin gated). Any tenant member who can see a published report
# may select it as their own active dashboard.


def _to_response(r: Report) -> ReportResponse:
    # Mirrors reports.py's _to_response (memory: response_model coercion for ORM
    # rows — ReportResponse.id is str, ORM Report.id is UUID, from_attributes does
    # NOT coerce). Duplicated rather than imported across modules: reports.py's
    # helper is private to that file and this module owns its own file set.
    return ReportResponse(
        id=str(r.id),
        title=r.title,
        status=r.status,
        version=r.version,
        created_at=r.created_at,
        has_recipe=r.recipe_json is not None,
        last_refreshed_at=r.last_refreshed_at,
        auto_refresh=r.auto_refresh,
        refresh_failure_count=r.refresh_failure_count,
        auto_refresh_paused_at=r.auto_refresh_paused_at,
        created_by=str(r.created_by) if r.created_by else None,
        dashboard_pinned_at=r.dashboard_pinned_at,
        # Rolling-period Stage 1 (Task 4): these two were missing here even though
        # Task 3 added them to ReportResponse — reports.py's own _to_response already
        # sets them. Filling them in is additive (both previously silently defaulted
        # to None on every dashboard response), not a change to any existing key.
        period=r.period,
        series_id=str(r.series_id) if r.series_id else None,
    )


def _period_sort_key(period: str | None) -> tuple[int, int]:
    """ "Mon YYYY" -> (year, month), sorting last (a very old year) for anything that
    doesn't parse -- defensive only (should never happen, see the module comment
    above); a malformed row must not be able to win "newest" by accident, and must
    never raise (this feeds a read path that must not 500)."""
    if period:
        parts = period.split(" ")
        if len(parts) == 2 and parts[0] in _MONTH_ABBRS and parts[1].isdigit():
            return int(parts[1]), _MONTH_ABBRS.index(parts[0]) + 1
    return -1, -1


def _newest_in_series(reports: list[Report]) -> Report | None:
    if not reports:
        return None
    # Period is the PRIMARY sort key (chronological, not created_at — see Task 4
    # brief: composing a backfilled month later must not make it lose to a month
    # composed earlier). created_at/id are tiebreaks only, relevant if two reports
    # somehow share a period (shouldn't happen — reports_series_id_period is a
    # partial unique index — but a tiebreak costs nothing and removes any
    # nondeterminism if it ever did).
    return max(reports, key=lambda r: (_period_sort_key(r.period), r.created_at, r.id))


async def _published_series(db: AsyncSession, tenant_id: uuid.UUID) -> list[tuple[ReportSeries, Report | None]]:
    """Every tracking series for the tenant, paired with its newest report (None if
    none composed yet — Task 3's tracking compose get-or-creates the series BEFORE the
    first report exists, so this is a real, expected transient state, not a bug).
    Two queries (not N+1): series don't carry a `published`/pin flag of their own —
    existing IS selectable, unlike a report which additionally needs
    `dashboard_pinned_at` set (see the module docstring)."""
    series_rows = (
        (
            await db.execute(
                select(ReportSeries)
                .where(ReportSeries.tenant_id == tenant_id)
                .order_by(ReportSeries.created_at.desc(), ReportSeries.id.desc())
            )
        )
        .scalars()
        .all()
    )
    if not series_rows:
        return []
    series_ids = [s.id for s in series_rows]
    report_rows = (
        (
            await db.execute(
                select(Report).where(
                    Report.tenant_id == tenant_id,
                    Report.series_id.in_(series_ids),
                    Report.period.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    by_series: dict[uuid.UUID, list[Report]] = {}
    for r in report_rows:
        by_series.setdefault(r.series_id, []).append(r)
    return [(s, _newest_in_series(by_series.get(s.id, []))) for s in series_rows]


def _to_series_response(series: ReportSeries, newest: Report | None) -> DashboardSeriesResponse:
    return DashboardSeriesResponse(
        id=str(series.id),
        playbook_key=series.playbook_key,
        period=newest.period if newest is not None else None,
        report_id=str(newest.id) if newest is not None else None,
    )


async def _build_tracking_info(
    db: AsyncSession, tenant_id: uuid.UUID, series: ReportSeries, active_report: Report | None
) -> DashboardTrackingInfo:
    """The ribbon's live-NetSuite half — AT MOST ONE call to
    `resolve_last_closed_period` per invocation (Task 4 brief), and MUST run before
    any `db.commit()` in the caller: it queries the (RLS-protected) Connection table
    (see netsuite_suiteql.execute), so calling it after a commit would silently see
    zero rows (constraint 3 — the SET LOCAL tenant GUC is cleared by COMMIT).

    Skips the call entirely when there's no report to contextualize yet (an empty
    series) — nothing to reconcile the live period against, so there is nothing
    worth asking NetSuite. `resolve_last_closed_period` itself never raises for
    "couldn't determine" (it returns an unresolved ClosedPeriod instead), but this
    still wraps the call: it does live I/O, and the Task 4 brief is explicit that a
    failure here must degrade the ribbon to "can't tell", never fail the whole GET/PUT.

    T2-gate MAJOR 2: calls the TTL-memoized `resolve_last_closed_period_cached`, not
    the raw resolver directly — this function is reached from THREE separate
    endpoints (GET /dashboard, PUT /active, the dismiss endpoint) and period close
    state changes at most once a month, so a live SuiteQL round trip on every one of
    those calls was pure waste. See period_resolver.py's own docstring for the
    cache's TTL/never-cache-a-failure contract.
    """
    if active_report is None:
        return DashboardTrackingInfo(series_id=str(series.id), playbook_key=series.playbook_key)

    from app.services.report.period_resolver import (  # lazy: monkeypatch boundary
        resolve_last_closed_period_cached,
    )

    try:
        closed = await resolve_last_closed_period_cached(db, tenant_id)
    except Exception:
        logger.warning("dashboard tracking: period_resolver call failed, degrading", exc_info=True)
        closed = None

    if closed is None or not closed.resolved:
        return DashboardTrackingInfo(
            series_id=str(series.id), playbook_key=series.playbook_key, period=active_report.period
        )
    # Only when the series is genuinely behind AND the sweep that would catch it up is
    # switched on. `closed_days_ago` drives a ribbon that says a statement "is
    # scheduled"; with ROLLING_PERIOD_AUTO_COMPOSE_ENABLED false nothing is scheduled,
    # and a UI promising work no job will do is the same defect the T2 gate caught in
    # the Stage 1 launcher copy. Gate the DATA, not just the wording.
    closed_days_ago = None
    if (
        # THE shared predicate, not a raw flag read. Gate round 2: this module checked
        # only ROLLING_PERIOD_AUTO_COMPOSE_ENABLED, so a cap of 0 (a plausible throttle
        # during a rate-limit incident) stopped every compose while this ribbon kept
        # promising one. Two modules reading a raw boolean is how that hole opened.
        settings.auto_compose_is_scheduled
        # BEHIND, not merely different. NetSuite periods can be REOPENED for corrections,
        # so the resolved close can regress to a period this series already holds — and
        # the sweep, which asks "does a report exist for closed.name", would find it
        # covered and skip it. Comparing for inequality alone promised a compose that
        # would never run, on a ribbon that would never clear (gate round 2).
        and _period_sort_key(active_report.period) < _period_sort_key(closed.name)
        and closed.enddate is not None
    ):
        # UTC, not the server's local wall clock: celery_app sets timezone="UTC" and the
        # sweep this number describes runs on that clock, so a local-time `today()` can
        # disagree with it by a day near midnight (T2 gate round 1).
        closed_days_ago = max((datetime.now(timezone.utc).date() - closed.enddate).days, 0)

    return DashboardTrackingInfo(
        series_id=str(series.id),
        playbook_key=series.playbook_key,
        period=active_report.period,
        period_check_ok=True,
        resolved_period=closed.name,
        closed_days_ago=closed_days_ago,
        next_open_period=closed.next_open_name,
    )


async def _published_reports(db: AsyncSession, tenant_id: uuid.UUID) -> list[Report]:
    rows = (
        (
            await db.execute(
                select(Report)
                .where(Report.tenant_id == tenant_id, Report.dashboard_pinned_at.is_not(None))
                # id DESC as a tiebreaker: two reports published in the same tick
                # otherwise have no deterministic order between requests, and the
                # fallback (published[0]) would inherit that nondeterminism.
                .order_by(Report.dashboard_pinned_at.desc(), Report.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _get_preference(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> UserDashboardPreference | None:
    return (
        await db.execute(
            select(UserDashboardPreference).where(
                UserDashboardPreference.tenant_id == tenant_id,
                UserDashboardPreference.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


async def _get_visible_report(db: AsyncSession, report_id: str, user: User) -> Report:
    # Same not-found shape as reports.py's _get_owned: malformed uuid, unknown id,
    # and a cross-tenant id are all indistinguishable 404s (no existence
    # disclosure). Unlike _get_owned, "visible" here does not imply "owned" — any
    # report in the caller's tenant may be selected, not just ones they created.
    try:
        rid = uuid.UUID(report_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    row = (
        await db.execute(select(Report).where(Report.id == rid, Report.tenant_id == user.tenant_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return row


async def _get_visible_series(db: AsyncSession, series_id: str, user: User) -> ReportSeries:
    """Same no-existence-disclosure 404 shape as `_get_visible_report` above (malformed
    id, unknown id, and a cross-tenant id are all indistinguishable 404s) — a series has
    no separate "published" concept to gate on (see `_published_series`'s docstring),
    so unlike `_get_visible_report` there is no follow-on 409 check here."""
    try:
        sid = uuid.UUID(series_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Series not found")
    row = (
        await db.execute(select(ReportSeries).where(ReportSeries.id == sid, ReportSeries.tenant_id == user.tenant_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Series not found")
    return row


async def _clear_stale_selection(db: AsyncSession, pref: UserDashboardPreference, user: User) -> bool:
    """Conditional delete-and-audit for a stale stored selection — generalized
    from a deleted-report-only tombstone GC to cover BOTH triggers of
    `active_is_fallback` (see `_resolve_active`'s own docstring): the selected
    report was deleted (`report_id IS NULL`, migration 092's ON DELETE SET
    NULL) or it was unpublished (`report_id` still points at a real report
    that is no longer in the published set). Dismissing the notice must clear
    either — a dismiss that only handled the deleted-report case left the
    unpublished case's banner reappearing on every subsequent load with no
    way to silence it.

    Not `db.delete(pref)` by primary key alone: between `pref` being loaded
    (by `_get_preference` in the caller) and this delete executing, a
    concurrent `PUT /dashboard/active` on another connection could have
    re-pointed the SAME row at a currently-published report — the upsert in
    `set_active_dashboard` updates `report_id` on the existing row via
    `ON CONFLICT DO UPDATE` rather than inserting a new one. An
    unconditional-by-pk delete would silently discard that fresh, valid
    selection along with the stale one, with no trace.

    The WHERE clause re-checks staleness against the LIVE `reports` table
    inside the same DELETE statement — not a `published` snapshot captured
    before the race — so it is correct regardless of what a concurrent writer
    did in between: a re-point to a published report, or the original report
    getting re-published, both make the delete a no-op, exactly like the
    original NULL-only check did for the tombstone case.

    The same conditional also makes this the loser-safe half of two
    concurrent GETs racing the same stale row: both load it, both call this,
    but only the winner's DELETE matches a row — the loser's matches zero and
    (per the `rowcount` gate below) does not audit a second
    `dashboard.stale_selection_cleared` event for what is a one-time notice.

    Rolling-period Stage 1 (Task 4): `UserDashboardPreference.series_id.is_(None)` is
    a REQUIRED extra guard, not decoration. Before Task 4, `report_id IS NULL` meant
    exactly one thing: a tombstoned (deleted) report. Task 4 makes `report_id IS NULL`
    the NORMAL state for a valid, non-stale SERIES selection too — without this guard
    the predicate below would treat every currently-tracking series selection as stale
    and delete it out from under the user on the very next dismiss. A series selection
    is never cleared by this report-focused staleness check (a deleted/tombstoned
    series is instead handled by `_resolve_active`'s own fallback branch, same as a
    tombstoned report).

    Returns True iff this call was the one that actually deleted the row
    (and therefore audited the clear).
    """
    result = await db.execute(
        delete(UserDashboardPreference).where(
            UserDashboardPreference.id == pref.id,
            UserDashboardPreference.series_id.is_(None),
            or_(
                UserDashboardPreference.report_id.is_(None),
                UserDashboardPreference.report_id.notin_(
                    select(Report.id).where(
                        Report.tenant_id == user.tenant_id,
                        Report.dashboard_pinned_at.is_not(None),
                    )
                ),
            ),
        )
    )
    if result.rowcount != 1:
        return False
    # Audited like any other mutation (rules/sqlalchemy-fastapi #4) even though
    # it is system GC on a read path: it is a real row deletion, and the audit
    # trail is what explains why the user's stored choice vanished. Gated on
    # rowcount so a concurrent loser (or a row re-pointed/re-published out
    # from under us) never logs a phantom clear.
    # category="dashboard" and the REPORT's id (not the preference row's PK) — matches
    # this module's other two audit calls (dashboard.select / dashboard.clear) so an
    # operator filtering GET /audit?category=dashboard sees the whole family, and so
    # resource_type="report" + resource_id actually resolves to a report. NULL report_id
    # (the deleted-report tombstone) has no report left to name, hence the None.
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="dashboard",
        action="dashboard.stale_selection_cleared",
        actor_id=user.id,
        resource_type="report",
        resource_id=str(pref.report_id) if pref.report_id is not None else None,
    )
    return True


async def _delete_preference_if_unchanged(db: AsyncSession, pref: UserDashboardPreference) -> bool:
    """Conditional delete-by-value for DELETE /dashboard/active. Same race as
    `_clear_stale_selection` above, guarded the same way (re-assert loaded
    state in the WHERE clause instead of `db.delete(pref)` by primary key
    alone): a concurrent `PUT /dashboard/active` on another connection can
    re-point this SAME row (the upsert in `set_active_dashboard` updates the
    existing row via `ON CONFLICT DO UPDATE`) between this row being loaded
    and this delete executing.

    Unlike `_clear_stale_selection` (which re-checks "is this NOW stale"
    against the live `reports` table), an explicit user DELETE has no
    staleness question to ask — it means "forget whatever is there" — so
    instead this re-asserts `report_id` (and, rolling-period Stage 1 Task 4:
    `series_id`) still equal the values it was loaded with. If a concurrent PUT
    changed either — including switching the row from a report selection to a
    series one or back — that is a genuinely fresh selection made after this
    DELETE was issued, and the WHERE clause makes the delete a no-op rather
    than silently discarding it.

    Returns True iff this call actually deleted the row (and therefore should
    audit the clear). Because the WHERE clause re-asserts both columns, a True
    result also guarantees the deleted row's `report_id`/`series_id` equalled
    `pref.report_id`/`pref.series_id` at the moment of deletion — so the
    caller's audit fields, read from the already-loaded `pref`, are never out
    of sync with what was actually deleted. The audit itself is the caller's
    responsibility (`clear_active_dashboard` below), not this helper's —
    unlike `_clear_stale_selection`, which is only ever called from one site
    and owns its own audit call.
    """
    where_clause = [UserDashboardPreference.id == pref.id]
    if pref.report_id is None:
        where_clause.append(UserDashboardPreference.report_id.is_(None))
    else:
        where_clause.append(UserDashboardPreference.report_id == pref.report_id)
    if pref.series_id is None:
        where_clause.append(UserDashboardPreference.series_id.is_(None))
    else:
        where_clause.append(UserDashboardPreference.series_id == pref.series_id)
    result = await db.execute(delete(UserDashboardPreference).where(*where_clause))
    return result.rowcount == 1


def _resolve_active(
    published: list[Report],
    series_pairs: list[tuple[ReportSeries, Report | None]],
    pref: UserDashboardPreference | None,
) -> tuple[Report | None, bool, ReportSeries | None]:
    """Shared "what does this user see" derivation, used by the pure GET (below), the
    dismiss endpoint's post-clear response, and PUT /active's series branch. Returns
    (active, active_is_fallback, tracking_series) — `tracking_series` is the
    currently-selected `ReportSeries` iff `active` (or its absence) came from a STILL-
    VALID tracking selection, so the caller knows whether to spend its one-per-request
    resolver call building `DashboardTrackingInfo` (mock §3: the ribbon renders ONLY
    for a genuine tracking selection, never for a fallback — a fallback always lands on
    a plain report, see below).

    `pref.report_id`/`pref.series_id` are None when the selected report/series was
    deleted (the FK tombstones via ON DELETE SET NULL, migrations 092/094) — that never
    equals a real row's id, so it funnels into the same "unavailable" branch as an
    unpublished-but-still-existing report selection. The CHECK constraint
    (`ck_user_dashboard_preference_one_selection`) guarantees at most one of
    report_id/series_id is ever set on a real row, so the branches below are mutually
    exclusive by construction, not by convention.
    """
    if pref is not None and pref.series_id is not None:
        pair = next((sp for sp in series_pairs if sp[0].id == pref.series_id), None)
        if pair is not None:
            series, newest = pair
            # A valid tracking selection is NEVER a fallback, even with no report yet
            # (an empty series) — the user's choice is still exactly what it was; there
            # is simply nothing composed for it yet (Task 4 brief: "a sane empty shape,
            # not a 500", not a "your selection went away" notice).
            return newest, False, series
        # series_id pointed at a series that no longer exists (tombstoned FK) — same
        # self-heal-to-most-recently-published-report story as an unpublished/deleted
        # report below; a fallback always lands on a REPORT, never another series (the
        # workspace-published set is the fallback pool, not "some other series").
        active = published[0] if published else None
        return active, active is not None, None
    if pref is not None and pref.report_id is not None:
        active = next((r for r in published if r.id == pref.report_id), None)
        if active is not None:
            return active, False, None  # their choice is still published
        active = published[0] if published else None
        # Only flag a fallback when there is an actual substitute to name: the
        # FE banner reads "...showing {title} instead", which cannot render
        # with no report at all — the empty state (Task 5) owns that surface
        # instead of a banner naming nothing.
        return active, active is not None, None
    if pref is not None:
        # A preference row exists but BOTH report_id and series_id are NULL — fully
        # tombstoned (whichever kind of selection it used to be is gone). Same
        # fallback story as the two branches above.
        active = published[0] if published else None
        return active, active is not None, None
    if published:
        return published[0], False, None  # never chosen — not a fallback
    return None, False, None


async def _build_dashboard_response(db: AsyncSession, user: User) -> DashboardResponse:
    """Pure read — GET must never write. A stale stored selection (the
    conditional-delete/audit machinery below in `_clear_stale_selection`) is a
    real mutation, so it must never fire as a side effect of a GET: this
    module also backs `PublishedDashboardsSection` on the (unrelated) /reports
    page via the same `["dashboard"]` FE query key, and a visit to THAT page
    consuming the one-time notice would silently rob the /dashboard page of the
    banner it exists to show. The stale selection is cleared only by the
    explicit POST /dashboard/notice/dismiss endpoint below."""
    published = await _published_reports(db, user.tenant_id)
    series_pairs = await _published_series(db, user.tenant_id)
    pref = await _get_preference(db, user.tenant_id, user.id)
    active, active_is_fallback, tracking_series = _resolve_active(published, series_pairs, pref)
    active_tracking = None
    if tracking_series is not None:
        active_tracking = await _build_tracking_info(db, user.tenant_id, tracking_series, active)
    return DashboardResponse(
        published=[_to_response(r) for r in published],
        published_series=[_to_series_response(s, r) for s, r in series_pairs],
        active=_to_response(active) if active is not None else None,
        active_is_fallback=active_is_fallback,
        active_tracking=active_tracking,
    )


@router.get("", response_model=DashboardResponse)
async def get_dashboard(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await _build_dashboard_response(db, user)


@router.post("/notice/dismiss", response_model=DashboardResponse)
async def dismiss_dashboard_notice(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Explicit, user-triggered consume of the stale-selection notice that GET
    (above) deliberately no longer touches. `active_is_fallback` has two
    triggers (see `_resolve_active`'s docstring): the selected report was
    deleted (report_id NULL) or unpublished (report_id still points at a
    real, now-unpublished report) — both must be dismissable, or the
    unpublished case's banner reappears on every subsequent load with no way
    to silence it. Idempotent: with no preference row, or one whose stored
    selection is currently valid (still published), `_clear_stale_selection`'s
    own predicate makes the delete a no-op, so this is a safe 200 either way
    rather than requiring a branch here. `_clear_stale_selection` itself never
    touches a series-type selection (see its own docstring, Task 4) — a valid
    tracking selection survives a dismiss untouched."""
    published = await _published_reports(db, user.tenant_id)  # pre-commit, reused below
    series_pairs = await _published_series(db, user.tenant_id)  # pre-commit, reused below
    pref = await _get_preference(db, user.tenant_id, user.id)

    cleared = False
    if pref is not None:
        cleared = await _clear_stale_selection(db, pref, user)

    # Resolve BEFORE commit — same reasoning as everywhere else in this module: when
    # the effective selection is a still-valid tracking one, _build_tracking_info's
    # resolver call queries an RLS-protected table and must run while the tenant GUC
    # is still set (constraint 3), i.e. before the db.commit() below clears it.
    effective_pref = None if cleared else pref
    active, active_is_fallback, tracking_series = _resolve_active(published, series_pairs, effective_pref)
    active_tracking = None
    if tracking_series is not None:
        active_tracking = await _build_tracking_info(db, user.tenant_id, tracking_series, active)

    await db.commit()

    # Build the response from data loaded/computed before the commit above
    # (constraint 4: never query an RLS table after db.commit(), since the SET
    # LOCAL tenant GUC is cleared by the commit).
    return DashboardResponse(
        published=[_to_response(r) for r in published],
        published_series=[_to_series_response(s, r) for s, r in series_pairs],
        active=_to_response(active) if active is not None else None,
        active_is_fallback=active_is_fallback,
        active_tracking=active_tracking,
    )


@router.put("/active", response_model=DashboardResponse)
async def set_active_dashboard(
    request: DashboardActiveRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    # Exactly one of report_id/series_id (rolling-period Stage 1, Task 4) — a 400 from
    # the handler itself, not pydantic's automatic 422, matching the brief. Checked
    # first, before any query: neither/both is a client error independent of whether
    # either id would even resolve.
    if (request.report_id is None) == (request.series_id is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide exactly one of report_id or series_id",
        )

    # fetch BEFORE commit — reused for the response below so no RLS-protected query
    # runs after the GUC-clearing commit (constraint 4). Both are needed on every
    # branch: `published_series` is now part of every DashboardResponse (mock §5's
    # switcher lists both kinds regardless of which kind was just selected).
    published = await _published_reports(db, user.tenant_id)
    series_pairs = await _published_series(db, user.tenant_id)

    now = datetime.now(timezone.utc)

    if request.report_id is not None:
        row = await _get_visible_report(db, request.report_id, user)
        if row.dashboard_pinned_at is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That report isn't published to the dashboard",
            )

        # Real Postgres upsert, not read-then-branch: two concurrent PUTs for the
        # same user (double-click, two tabs) both racing a `_get_preference`
        # SELECT would both see "no row", both INSERT, and the loser would
        # violate uq_user_dashboard_preference_tenant_user as an unhandled
        # IntegrityError -> 500. ON CONFLICT DO UPDATE makes the second writer a
        # winning UPDATE instead of a losing INSERT — atomic at the DB level, no
        # read-modify-write race window. series_id is explicitly cleared on both
        # the insert and the conflict-update: a prior series selection left on the
        # row would otherwise violate ck_user_dashboard_preference_one_selection
        # the moment report_id is also set.
        upsert_stmt = pg_insert(UserDashboardPreference).values(
            tenant_id=user.tenant_id,
            user_id=user.id,
            report_id=row.id,
            series_id=None,
            updated_at=now,
        )
        upsert_stmt = upsert_stmt.on_conflict_do_update(
            constraint="uq_user_dashboard_preference_tenant_user",
            set_={"report_id": row.id, "series_id": None, "updated_at": now},
        )
        await db.execute(upsert_stmt)

        await audit_service.log_event(
            db=db,
            tenant_id=user.tenant_id,
            category="dashboard",
            action="dashboard.select",
            actor_id=user.id,
            resource_type="report",
            resource_id=str(row.id),
        )
        await db.commit()
        # No post-commit query/refresh: `row` (the just-selected report, already
        # published per the 409 check above) and `published`/`series_pairs` were all
        # loaded before commit; expire_on_commit=False keeps their attributes
        # readable. Re-querying here would run in the transaction the commit just
        # closed, where SET LOCAL has been cleared — see constraint 4.
        return DashboardResponse(
            published=[_to_response(r) for r in published],
            published_series=[_to_series_response(s, r) for s, r in series_pairs],
            active=_to_response(row),
            active_is_fallback=False,
            active_tracking=None,
        )

    # request.series_id branch (rolling-period Stage 1, Task 4).
    series = await _get_visible_series(db, request.series_id, user)
    newest = next((r for s, r in series_pairs if s.id == series.id), None)
    # MUST run before the upsert's commit below: the resolver call queries an
    # RLS-protected table (see _build_tracking_info's docstring) and would silently
    # see zero rows / report unreachable if it ran after the GUC-clearing commit
    # (constraint 3).
    active_tracking = await _build_tracking_info(db, user.tenant_id, series, newest)

    # Defense in depth (T2-gate MAJOR 1): _build_tracking_info's resolver call already
    # restores tenant context itself before returning (period_resolver.py's own
    # `finally`), but this re-set right before OUR OWN RLS-protected write is a second,
    # independent guard against the same bug class — this codebase has been bitten by
    # a mid-request commit silently clearing the tenant GUC three times now. Mirrors
    # playbooks.py's "tool calls may commit" re-set immediately before its Report
    # insert.
    await set_tenant_context(db, str(user.tenant_id))

    upsert_stmt = pg_insert(UserDashboardPreference).values(
        tenant_id=user.tenant_id,
        user_id=user.id,
        report_id=None,
        series_id=series.id,
        updated_at=now,
    )
    upsert_stmt = upsert_stmt.on_conflict_do_update(
        constraint="uq_user_dashboard_preference_tenant_user",
        set_={"report_id": None, "series_id": series.id, "updated_at": now},
    )
    await db.execute(upsert_stmt)

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="dashboard",
        action="dashboard.select",
        actor_id=user.id,
        resource_type="series",
        resource_id=str(series.id),
    )
    await db.commit()
    return DashboardResponse(
        published=[_to_response(r) for r in published],
        published_series=[_to_series_response(s, r) for s, r in series_pairs],
        active=_to_response(newest) if newest is not None else None,
        active_is_fallback=False,
        active_tracking=active_tracking,
    )


@router.delete("/active", response_model=DashboardResponse)
async def clear_active_dashboard(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    published = await _published_reports(db, user.tenant_id)  # pre-commit, reused below
    series_pairs = await _published_series(db, user.tenant_id)  # pre-commit, reused below

    pref = await _get_preference(db, user.tenant_id, user.id)
    cleared = False
    if pref is not None:
        # Conditional delete (`_delete_preference_if_unchanged`), not
        # `db.delete(pref)` by primary key alone: a concurrent
        # `PUT /dashboard/active` can re-point this same row between this
        # load and the delete below (see the helper's docstring). An
        # unconditional delete would silently discard that fresh selection.
        cleared = await _delete_preference_if_unchanged(db, pref)

    if cleared:
        # pref.report_id/series_id may already be None (tombstoned by a report/series
        # delete, migrations 092/094's ON DELETE SET NULL) — str(None) would log the
        # literal string "None" as a resource_id, so only stringify a real id. Safe to
        # read from the already-loaded `pref` (not a post-delete re-query): `cleared`
        # is only True because the row's report_id/series_id still matched these exact
        # values at the moment of deletion, so this can never disagree with what was
        # actually deleted. The CHECK constraint guarantees at most one of the two is
        # ever non-null, so the resource_type below is unambiguous.
        if pref.series_id is not None:
            cleared_resource_type, cleared_resource_id = "series", str(pref.series_id)
        elif pref.report_id is not None:
            cleared_resource_type, cleared_resource_id = "report", str(pref.report_id)
        else:
            cleared_resource_type, cleared_resource_id = "report", None
        await audit_service.log_event(
            db=db,
            tenant_id=user.tenant_id,
            category="dashboard",
            action="dashboard.clear",
            actor_id=user.id,
            resource_type=cleared_resource_type,
            resource_id=cleared_resource_id,
        )
    # Gated on `cleared` (rules/sqlalchemy-fastapi #4 still applies to a real
    # deletion, but not to a no-op): a repeat DELETE with nothing to clear, or
    # one that raced a concurrent PUT and lost, must not write an audit row
    # claiming a clear that didn't happen.
    await db.commit()
    # After an explicit, successful clear the user has no stored selection at
    # all (not merely a stale one) — same as "never chosen", so
    # active_is_fallback is False here even though the wall now shows the
    # most-recently-published fallback. Same response shape when `cleared` is
    # False: either there was nothing stored to begin with, or a concurrent
    # PUT already won the race and its fresher selection stands — this
    # caller's DELETE simply has nothing left to report, and the next GET
    # reflects the current truth. The fallback always lands on a REPORT (never a
    # series — see _resolve_active's docstring), so active_tracking is always None
    # here, same as every other fallback branch in this module.
    active = published[0] if published else None
    return DashboardResponse(
        published=[_to_response(r) for r in published],
        published_series=[_to_series_response(s, r) for s, r in series_pairs],
        active=_to_response(active) if active is not None else None,
        active_is_fallback=False,
        active_tracking=None,
    )
