import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.report import Report
from app.models.report_version import ReportVersion
from app.models.user import User
from app.schemas.report import ReportResponse, ReportSettingsUpdate, ReportVersionResponse
from app.services import audit_service
from app.services.report import refresh_service
from app.services.report.playbooks import PLAYBOOKS, compose_playbook_report

router = APIRouter(prefix="/reports", tags=["reports"])


def _to_response(r: Report) -> ReportResponse:
    # ReportResponse.id is a str; ORM Report.id is a UUID and from_attributes
    # does NOT coerce UUID -> str (memory: response_model coercion for ORM rows),
    # so build the response with the id stringified explicitly.
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
    )


def _can_manage(user: User, row: Report) -> bool:
    """Creator-or-admin gate for destructive report actions (delete/pin)."""
    if row.created_by is not None and row.created_by == user.id:
        return True
    return any(ur.role.name == "admin" for ur in user.user_roles)


@router.get("", response_model=list[ReportResponse])
async def list_reports(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rows = (await db.execute(select(Report).order_by(Report.created_at.desc()))).scalars().all()
    return [_to_response(r) for r in rows]


class PlaybookComposeRequest(BaseModel):
    params: dict[str, str] = {}


@router.get("/playbooks")
async def list_playbooks(
    user: Annotated[User, Depends(get_current_user)],
):
    return [
        {"key": key, "name": m["name"], "description": m["description"], "params": m["params"]}
        for key, m in PLAYBOOKS.items()
    ]


@router.post("/playbooks/{playbook_key}", response_model=ReportResponse, status_code=status.HTTP_201_CREATED)
async def compose_playbook_endpoint(
    playbook_key: str,
    request: PlaybookComposeRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if playbook_key not in PLAYBOOKS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown playbook")
    try:
        report = await compose_playbook_report(
            db,
            playbook_key=playbook_key,
            params=request.params,
            tenant_id=user.tenant_id,
            actor_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except refresh_service.RefreshError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e
    return _to_response(report)


async def _get_owned(db: AsyncSession, report_id: str) -> Report:
    try:
        rid = uuid.UUID(report_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    row = (await db.execute(select(Report).where(Report.id == rid))).scalar_one_or_none()
    if row is None:  # RLS-invisible cross-tenant rows land here too → 404 (no existence disclosure)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return row


@router.get("/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return _to_response(await _get_owned(db, report_id))


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_report(
    report_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _get_owned(db, report_id)
    if not _can_manage(user, row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the report's creator or a workspace admin can delete this report",
        )
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="report",
        action="report.delete",
        actor_id=user.id,
        resource_type="report",
        resource_id=str(row.id),
        payload={"title": row.title, "versions": row.version},
    )
    # report_versions.report_id has ondelete="CASCADE" — no ORM relationship exists,
    # so the DB removes version rows itself; do not add one here.
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{report_id}/view")
async def view_report(
    report_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _get_owned(db, report_id)
    return Response(content=row.rendered_html, media_type="text/html")


# --- Slice B (live-dashboard reports): manual refresh + version history ----------------
# Permission note (spec §6.3 "any viewer with report READ permission may refresh"): no
# report.* permission scope exists anywhere today — every report route is gated by
# get_current_user + RLS, so refresh uses EXACTLY what gates viewing. If a report.read
# scope is ever introduced, it must gate view/list/versions/refresh together.


@router.post("/{report_id}/refresh", response_model=ReportResponse)
async def refresh_report_endpoint(
    report_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _get_owned(db, report_id)  # 404 shape identical to existing routes
    try:
        # the CALLER's tenant, never row.tenant_id — under RLS they are equal, but if
        # RLS were ever bypassed the row's value could set a foreign tenant context.
        updated = await refresh_service.refresh_report(db, report_id=row.id, tenant_id=user.tenant_id, actor_id=user.id)
    except refresh_service.RefreshDebouncedError as e:
        raise HTTPException(
            status_code=e.status_code, detail=e.detail, headers={"Retry-After": str(e.retry_after_seconds)}
        ) from e
    except refresh_service.RefreshError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e
    return _to_response(updated)


# --- Slice C: auto-refresh settings + one-click resume ---------------------------------
# Same gate as every report route (get_current_user + RLS — see the §6.3 permission
# note above): whoever can view a report can schedule/resume its read-only replay.


@router.patch("/{report_id}/settings", response_model=ReportResponse)
async def update_report_settings(
    report_id: str,
    request: ReportSettingsUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _get_owned(db, report_id)
    # Legacy/snapshot reports stay snapshot-only (§6.1): recipe_json loads as Python
    # None for BOTH SQL NULL and the jsonb-'null' rows compose writes explicitly.
    if request.auto_refresh != "off" and row.recipe_json is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="snapshot-only report — no refresh recipe to schedule",
        )
    row.auto_refresh = request.auto_refresh
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="report",
        action="report.settings_update",
        actor_id=user.id,
        resource_type="report",
        resource_id=str(row.id),
        payload={"auto_refresh": request.auto_refresh},
    )
    await db.commit()
    # no db.refresh: the commit cleared the RLS GUC and expire_on_commit=False keeps
    # every attribute the response needs.
    return _to_response(row)


@router.post("/{report_id}/auto-refresh/resume", response_model=ReportResponse)
async def resume_auto_refresh(
    report_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """One-click resume after the failure ladder paused a report (§4C). Clears the
    pause AND zeroes the count — otherwise one stale failure would re-pause almost
    immediately. Idempotent on a never-paused report."""
    row = await _get_owned(db, report_id)
    row.auto_refresh_paused_at = None
    row.refresh_failure_count = 0
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="report",
        action="report.auto_refresh_resumed",
        actor_id=user.id,
        resource_type="report",
        resource_id=str(row.id),
    )
    await db.commit()
    return _to_response(row)


def _version_entry(v: ReportVersion, current_version: int) -> ReportVersionResponse:
    return ReportVersionResponse(
        version=v.version,
        created_at=v.created_at,
        created_by=str(v.created_by) if v.created_by else None,
        pinned=v.pinned,
        is_current=v.version == current_version,
    )


@router.get("/{report_id}/versions", response_model=list[ReportVersionResponse])
async def list_report_versions(
    report_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _get_owned(db, report_id)
    versions = (
        (
            await db.execute(
                select(ReportVersion).where(ReportVersion.report_id == row.id).order_by(ReportVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )
    if not versions:
        # never-refreshed report: synthesize the single "v1 · current" entry from the parent
        return [
            ReportVersionResponse(
                version=row.version,
                created_at=row.created_at,
                created_by=str(row.created_by) if row.created_by else None,
                pinned=False,
                is_current=True,
            )
        ]
    return [_version_entry(v, row.version) for v in versions]


@router.get("/{report_id}/versions/{version}/view")
async def view_report_version(
    report_id: str,
    version: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _get_owned(db, report_id)
    snapshot = (
        await db.execute(
            select(ReportVersion).where(ReportVersion.report_id == row.id, ReportVersion.version == version)
        )
    ).scalar_one_or_none()
    if snapshot is not None:
        return Response(content=snapshot.rendered_html, media_type="text/html")
    if version == row.version:  # never-refreshed report: the parent IS v1
        return Response(content=row.rendered_html, media_type="text/html")
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
