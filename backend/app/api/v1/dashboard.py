import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
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


async def _clear_tombstone_if_still_null(db: AsyncSession, tombstone: UserDashboardPreference, user: User) -> bool:
    """Conditional delete-and-audit for a deleted-report tombstone. Not
    `db.delete(tombstone)` by primary key alone: between `tombstone` being
    loaded (by `_get_preference` in `_build_dashboard_response`, below) and
    this delete executing, a concurrent `PUT /dashboard/active` on another
    connection could have re-pointed the SAME row at a real report — the
    upsert in `set_active_dashboard` updates `report_id` on the existing row
    via `ON CONFLICT DO UPDATE` rather than inserting a new one. An
    unconditional-by-pk delete would silently discard that fresh selection
    along with the tombstone, with no trace. Re-asserting `report_id IS NULL`
    in the WHERE clause makes the delete a no-op in that case.

    The same conditional also makes this the loser-safe half of two
    concurrent GETs racing the same stale tombstone: both load it, both call
    this, but only the winner's DELETE matches a row — the loser's matches
    zero and (per the `rowcount` gate below) does not audit a second
    `dashboard.tombstone_cleared` event for what is a one-time notice.

    Returns True iff this call was the one that actually deleted the row
    (and therefore audited the clear).
    """
    result = await db.execute(
        delete(UserDashboardPreference).where(
            UserDashboardPreference.id == tombstone.id,
            UserDashboardPreference.report_id.is_(None),
        )
    )
    if result.rowcount != 1:
        return False
    # Audited like any other mutation (rules/sqlalchemy-fastapi #4) even though
    # it is system GC on a read path: it is a real row deletion, and the audit
    # trail is what explains why the user's stored choice vanished. Gated on
    # rowcount so a concurrent loser (or a row re-pointed out from under us)
    # never logs a phantom clear.
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="report",
        action="dashboard.tombstone_cleared",
        actor_id=user.id,
        resource_type="report",
        resource_id=str(tombstone.id),
    )
    return True


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
    """Pure read — GET must never write. A deleted-report tombstone (the
    conditional-delete/audit machinery below in `_clear_tombstone_if_still_null`)
    is a real mutation, so it must never fire as a side effect of a GET: this
    module also backs `PublishedDashboardsSection` on the (unrelated) /reports
    page via the same `["dashboard"]` FE query key, and a visit to THAT page
    consuming the one-time notice would silently rob the /dashboard page of the
    banner it exists to show. The tombstone is cleared only by the explicit
    POST /dashboard/notice/dismiss endpoint below."""
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
    """Explicit, user-triggered consume of the deleted-report tombstone that
    GET (above) deliberately no longer touches. Idempotent: with no
    preference row, or one that isn't a tombstone (report_id still points at
    a real report — the unpublished case), `_clear_tombstone_if_still_null`'s
    own `report_id IS NULL` predicate makes the delete a no-op, so this is a
    safe 200 either way rather than requiring a branch here."""
    published = await _published_reports(db, user.tenant_id)  # pre-commit, reused below
    pref = await _get_preference(db, user.tenant_id, user.id)

    cleared = False
    if pref is not None:
        cleared = await _clear_tombstone_if_still_null(db, pref, user)
    await db.commit()

    # Build the response from data loaded before the commit above (constraint
    # 4: never query an RLS table after db.commit(), since the SET LOCAL
    # tenant GUC is cleared by the commit). When the row was actually cleared,
    # the user now has no stored selection at all — same post-clear shape as
    # DELETE /active (fall back to the most recent publish, not a fallback
    # notice since there is no longer an old choice to have lost). Otherwise
    # (no pref, or a no-op because it wasn't a tombstone) resolve normally
    # against the still-valid, already-loaded `pref` object.
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
    cleared_report_id: str | None = None
    if pref is not None:
        # pref.report_id may already be None (tombstoned by a report delete,
        # migration 092's ON DELETE SET NULL) — str(None) would log the
        # literal string "None" as a resource_id, so only stringify a real id.
        cleared_report_id = str(pref.report_id) if pref.report_id is not None else None
        await db.delete(pref)

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="dashboard",
        action="dashboard.clear",
        actor_id=user.id,
        resource_type="report",
        resource_id=cleared_report_id,
    )
    await db.commit()
    # After an explicit clear the user has no stored selection at all (not merely
    # a stale one) — same as "never chosen", so active_is_fallback is False here
    # even though the wall now shows the most-recently-published fallback.
    active = published[0] if published else None
    return DashboardResponse(
        published=[_to_response(r) for r in published],
        active=_to_response(active) if active is not None else None,
        active_is_fallback=False,
    )
