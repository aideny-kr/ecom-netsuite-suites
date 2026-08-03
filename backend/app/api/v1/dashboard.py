import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.report import Report
from app.models.user import User
from app.models.user_dashboard_preference import UserDashboardPreference
from app.schemas.dashboard import DashboardActiveRequest, DashboardResponse
from app.schemas.report import ReportResponse
from app.services import audit_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

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
    `dashboard.tombstone_cleared` event for what is a one-time notice.

    Returns True iff this call was the one that actually deleted the row
    (and therefore audited the clear).
    """
    result = await db.execute(
        delete(UserDashboardPreference).where(
            UserDashboardPreference.id == pref.id,
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
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="report",
        action="dashboard.tombstone_cleared",
        actor_id=user.id,
        resource_type="report",
        resource_id=str(pref.id),
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
    instead this re-asserts `report_id` still equals the value it was loaded
    with. If a concurrent PUT changed it, that is a genuinely fresh selection
    made after this DELETE was issued, and the WHERE clause makes the delete
    a no-op rather than silently discarding it.

    Returns True iff this call actually deleted the row (and therefore should
    audit the clear). Because the WHERE clause re-asserts `report_id`, a True
    result also guarantees the deleted row's `report_id` equalled `pref.report_id`
    at the moment of deletion — so the caller's audit `resource_id`, read from
    the already-loaded `pref`, is never out of sync with what was actually
    deleted. The audit itself is the caller's responsibility (`clear_active_dashboard`
    below), not this helper's — unlike `_clear_stale_selection`, which is only
    ever called from one site and owns its own audit call.
    """
    where_clause = [UserDashboardPreference.id == pref.id]
    if pref.report_id is None:
        where_clause.append(UserDashboardPreference.report_id.is_(None))
    else:
        where_clause.append(UserDashboardPreference.report_id == pref.report_id)
    result = await db.execute(delete(UserDashboardPreference).where(*where_clause))
    return result.rowcount == 1


def _resolve_active(published: list[Report], pref: UserDashboardPreference | None) -> tuple[Report | None, bool]:
    """Shared "what does this user see" derivation, used both by the pure GET
    (below) and by the dismiss endpoint's post-clear response. Returns
    (active, active_is_fallback).

    `pref.report_id` is None when the selected report was deleted (the FK
    tombstones via ON DELETE SET NULL, migration 092) — that never equals a
    real report's id, so it funnels into the same "unavailable" branch as an
    unpublished-but-still-existing selection.
    """
    if pref is not None:
        active = next((r for r in published if r.id == pref.report_id), None)
        if active is not None:
            return active, False  # their choice is still published
        # Stored selection unavailable — either unpublished (report_id still
        # points at a real, now-unpublished report) or deleted (report_id is
        # NULL). Fall back to the most recently published report.
        active = published[0] if published else None
        # Only flag a fallback when there is an actual substitute to name: the
        # FE banner reads "...showing {title} instead", which cannot render
        # with no report at all — the empty state (Task 5) owns that surface
        # instead of a banner naming nothing.
        return active, active is not None
    if published:
        return published[0], False  # never chosen — not a fallback
    return None, False


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
    pref = await _get_preference(db, user.tenant_id, user.id)
    active, active_is_fallback = _resolve_active(published, pref)
    return DashboardResponse(
        published=[_to_response(r) for r in published],
        active=_to_response(active) if active is not None else None,
        active_is_fallback=active_is_fallback,
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
    rather than requiring a branch here."""
    published = await _published_reports(db, user.tenant_id)  # pre-commit, reused below
    pref = await _get_preference(db, user.tenant_id, user.id)

    cleared = False
    if pref is not None:
        cleared = await _clear_stale_selection(db, pref, user)
    await db.commit()

    # Build the response from data loaded before the commit above (constraint
    # 4: never query an RLS table after db.commit(), since the SET LOCAL
    # tenant GUC is cleared by the commit). When the row was actually cleared,
    # the user now has no stored selection at all — same post-clear shape as
    # DELETE /active (fall back to the most recent publish, not a fallback
    # notice since there is no longer an old choice to have lost). Otherwise
    # (no pref, or a no-op because the stored selection is currently valid)
    # resolve normally against the still-valid, already-loaded `pref` object.
    effective_pref = None if cleared else pref
    active, active_is_fallback = _resolve_active(published, effective_pref)
    return DashboardResponse(
        published=[_to_response(r) for r in published],
        active=_to_response(active) if active is not None else None,
        active_is_fallback=active_is_fallback,
    )


@router.put("/active", response_model=DashboardResponse)
async def set_active_dashboard(
    request: DashboardActiveRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _get_visible_report(db, request.report_id, user)
    if row.dashboard_pinned_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That report isn't published to the dashboard",
        )

    # fetch the published set BEFORE commit — reused for the response below so no
    # RLS-protected query runs after the GUC-clearing commit (constraint 4).
    published = await _published_reports(db, user.tenant_id)

    # Real Postgres upsert, not read-then-branch: two concurrent PUTs for the
    # same user (double-click, two tabs) both racing a `_get_preference`
    # SELECT would both see "no row", both INSERT, and the loser would
    # violate uq_user_dashboard_preference_tenant_user as an unhandled
    # IntegrityError -> 500. ON CONFLICT DO UPDATE makes the second writer a
    # winning UPDATE instead of a losing INSERT — atomic at the DB level, no
    # read-modify-write race window.
    now = datetime.now(timezone.utc)
    upsert_stmt = pg_insert(UserDashboardPreference).values(
        tenant_id=user.tenant_id,
        user_id=user.id,
        report_id=row.id,
        updated_at=now,
    )
    upsert_stmt = upsert_stmt.on_conflict_do_update(
        constraint="uq_user_dashboard_preference_tenant_user",
        set_={"report_id": row.id, "updated_at": now},
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
    # published per the 409 check above) and `published` were both loaded before
    # commit; expire_on_commit=False keeps their attributes readable. Re-querying
    # here would run in the transaction the commit just closed, where SET LOCAL
    # has been cleared — see constraint 4.
    return DashboardResponse(
        published=[_to_response(r) for r in published],
        active=_to_response(row),
        active_is_fallback=False,
    )


@router.delete("/active", response_model=DashboardResponse)
async def clear_active_dashboard(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    published = await _published_reports(db, user.tenant_id)  # pre-commit, reused below

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
        # pref.report_id may already be None (tombstoned by a report delete,
        # migration 092's ON DELETE SET NULL) — str(None) would log the
        # literal string "None" as a resource_id, so only stringify a real
        # id. Safe to read from the already-loaded `pref` (not a post-delete
        # re-query): `cleared` is only True because the row's report_id still
        # matched this exact value at the moment of deletion, so this can
        # never disagree with what was actually deleted.
        cleared_report_id = str(pref.report_id) if pref.report_id is not None else None
        await audit_service.log_event(
            db=db,
            tenant_id=user.tenant_id,
            category="dashboard",
            action="dashboard.clear",
            actor_id=user.id,
            resource_type="report",
            resource_id=cleared_report_id,
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
    # reflects the current truth.
    active = published[0] if published else None
    return DashboardResponse(
        published=[_to_response(r) for r in published],
        active=_to_response(active) if active is not None else None,
        active_is_fallback=False,
    )
