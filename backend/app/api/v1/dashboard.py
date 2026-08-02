import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
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


async def _build_dashboard_response(db: AsyncSession, user: User) -> DashboardResponse:
    """Read-only-ish assembly used by GET only — PUT/DELETE build their response
    from data already loaded pre-commit (see constraint 4: never query an RLS
    table after db.commit(), since the SET LOCAL tenant GUC is cleared by the
    commit and the tenant policy would silently filter every row rather than
    500). GET follows the same discipline for its own conditional commit below
    (the deleted-report tombstone self-heal): build the full response from
    already-loaded data FIRST, then delete + commit, then return without
    querying anything else."""
    published = await _published_reports(db, user.tenant_id)
    pref = await _get_preference(db, user.tenant_id, user.id)

    active: Report | None = None
    active_is_fallback = False
    tombstone_to_clear: UserDashboardPreference | None = None

    if pref is not None:
        # `pref.report_id` is None when the selected report was deleted (the
        # FK tombstones via ON DELETE SET NULL, migration 092) — that never
        # equals a real report's id, so it funnels into the same "unavailable"
        # branch as an unpublished-but-still-existing selection below.
        active = next((r for r in published if r.id == pref.report_id), None)
        if active is None:
            # Stored selection unavailable — either unpublished (report_id
            # still points at a real, now-unpublished report) or deleted
            # (report_id is NULL). Self-heal on READ: do not delete or repair
            # an unpublished-report row here — that report can be re-published,
            # so the row must keep remembering the user's choice.
            active = published[0] if published else None
            # Only flag a fallback when there is an actual substitute to name:
            # the FE banner reads "...showing {title} instead", which cannot
            # render with no report at all — the empty state (Task 5) owns that
            # surface instead of a banner naming nothing.
            active_is_fallback = active is not None
            if active_is_fallback and pref.report_id is None:
                # A deleted-report tombstone, by contrast, is a permanent dead
                # end — report_id can never again match a real published report
                # — so the banner would otherwise reappear on every load
                # forever. Reporting it once (this response) and clearing the
                # row here is what makes it a one-time notice.
                tombstone_to_clear = pref
        # else: their choice is still published — active_is_fallback stays False.
    elif published:
        active = published[0]  # never chosen — not a fallback

    response = DashboardResponse(
        published=[_to_response(r) for r in published],
        active=_to_response(active) if active is not None else None,
        active_is_fallback=active_is_fallback,
    )

    if tombstone_to_clear is not None:
        await db.delete(tombstone_to_clear)
        await db.commit()

    return response


@router.get("", response_model=DashboardResponse)
async def get_dashboard(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await _build_dashboard_response(db, user)


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
